"""任务接口：创建、查询、取消，以及 WebSocket 实时日志。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from app.api.deps import get_jobs
from app.models.api import JobCreateRequest, JobDetailResponse, JobListResponse
from app.models.job import JobEvent, JobRecord
from app.services.jobs.manager import JobManager

router = APIRouter(tags=["jobs"])


@router.post("/jobs", response_model=JobDetailResponse, status_code=202)
async def create_job(
    payload: JobCreateRequest,
    jobs: JobManager = Depends(get_jobs),
) -> JobDetailResponse:
    """提交任务。传 ``prompt`` 则先生成方案再执行，传 ``plan`` 则直接执行。"""

    record = await jobs.create(payload)
    return JobDetailResponse(job=record)


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    limit: int = 50,
    jobs: JobManager = Depends(get_jobs),
) -> JobListResponse:
    return JobListResponse(jobs=jobs.list_jobs(limit=max(1, min(limit, 200))))


@router.get("/jobs/{job_id}", response_model=JobDetailResponse)
async def get_job(job_id: str, jobs: JobManager = Depends(get_jobs)) -> JobDetailResponse:
    try:
        return JobDetailResponse(job=jobs.get_job(job_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"任务不存在：{job_id}") from exc


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, jobs: JobManager = Depends(get_jobs)) -> dict[str, object]:
    try:
        record = jobs.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"任务不存在：{job_id}") from exc
    return {"job_id": job_id, "status": record.status.value, "events": record.events}


@router.post("/jobs/{job_id}/cancel", response_model=JobDetailResponse)
async def cancel_job(job_id: str, jobs: JobManager = Depends(get_jobs)) -> JobDetailResponse:
    try:
        record = await jobs.cancel(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"任务不存在：{job_id}") from exc
    return JobDetailResponse(job=record)


@router.websocket("/ws/jobs/{job_id}")
async def job_stream(websocket: WebSocket, job_id: str) -> None:
    jobs: JobManager = websocket.app.state.jobs
    try:
        record: JobRecord = jobs.get_job(job_id)
    except KeyError:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    queue = jobs.subscribe(job_id)
    try:
        await websocket.send_json(
            {"type": "snapshot", "job": json.loads(record.model_dump_json(exclude={"events"}))}
        )
        for event in list(record.events):
            await websocket.send_json({"type": "event", "event": _event_payload(event)})
        if record.status.value in {"succeeded", "failed", "cancelled"}:
            await websocket.send_json({"type": "done", "status": record.status.value})
            return

        while True:
            event = await queue.get()
            if event is None:
                await websocket.send_json({"type": "done", "status": record.status.value})
                break
            await websocket.send_json({"type": "event", "event": _event_payload(event)})
    except WebSocketDisconnect:
        pass
    finally:
        jobs.unsubscribe(job_id, queue)


def _event_payload(event: JobEvent) -> dict:
    return json.loads(event.model_dump_json())
