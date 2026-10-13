"""任务管理：把「生成方案 + 执行方案」跑成可观测的后台任务。

* 任务串行执行（同一时间只操作一个 Premiere 实例）；
* 每个任务一个事件流，前端通过 WebSocket 实时接收；
* 所有产物落在 ``workspace/runs/<job_id>/``，方便复盘。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings
from app.core.errors import PlanValidationError
from app.core.logging import get_logger
from app.models.api import JobCreateRequest, PlanRequest
from app.models.job import (
    EventLevel,
    JobEvent,
    JobRecord,
    JobStatus,
    JobSummary,
    utc_now,
)
from app.models.media import MediaLibrary
from app.models.plan import ExportSequenceOp, OperationType, EditPlan
from app.services.executor import PlanExecutor
from app.services.media_scanner import get_library
from app.services.planner.service import PlannerService
from app.services.premiere.base import PremiereAdapter

logger = get_logger(__name__)

MAX_EVENTS_KEPT = 2000


class JobManager:
    def __init__(
        self,
        settings: Settings,
        planner: PlannerService,
        adapter: PremiereAdapter,
    ) -> None:
        self.settings = settings
        self.planner = planner
        self.adapter = adapter
        self._jobs: dict[str, JobRecord] = {}
        self._event_seq: dict[str, int] = {}
        self._queues: dict[str, list[asyncio.Queue]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- 查询
    def list_jobs(self, limit: int = 50) -> list[JobSummary]:
        records = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
        return [JobSummary.from_record(record) for record in records[:limit]]

    def get_job(self, job_id: str) -> JobRecord:
        record = self._jobs.get(job_id)
        if record is None:
            raise KeyError(job_id)
        return record

    # ---------------------------------------------------------------- 创建/执行
    async def create(self, request: JobCreateRequest) -> JobRecord:
        if request.plan is None and not (request.prompt or "").strip():
            raise PlanValidationError("请求里必须提供 prompt 或 plan")

        record = JobRecord(
            media_root=request.media_root,
            dry_run=request.dry_run,
            continue_on_error=request.continue_on_error,
            plan=request.plan,
        )
        run_dir = self.settings.runs_dir / record.id
        run_dir.mkdir(parents=True, exist_ok=True)
        record.run_dir = str(run_dir)

        self._jobs[record.id] = record
        self._event_seq[record.id] = 0
        self._persist(record)
        self._tasks[record.id] = asyncio.create_task(
            self._run(record, request), name=f"aipr-job-{record.id}"
        )
        logger.info("创建任务 %s（media_root=%s）", record.id, request.media_root)
        return record

    async def _run(self, record: JobRecord, request: JobCreateRequest) -> None:
        emit = self._make_emitter(record)
        async with self._lock:  # 同一时间只允许一个任务操作 Premiere
            record.status = JobStatus.RUNNING
            record.started_at = utc_now()
            try:
                await emit(EventLevel.INFO, "任务开始执行")
                library = await self._load_library(request)
                await emit(
                    EventLevel.INFO,
                    f"素材库：{len(library.assets)} 个文件"
                    + (f"（{library.root}）" if library.root else ""),
                )

                plan = record.plan
                if plan is None:
                    await emit(EventLevel.INFO, "正在生成剪辑方案…")
                    response = await self.planner.create_plan(
                        PlanRequest(
                            prompt=request.prompt or "",
                            media_root=request.media_root or "",
                            media_ids=request.media_ids,
                            options=request.options,
                        ),
                        library,
                    )
                    plan = response.plan
                    record.plan = plan
                    self._write_json(record, "plan.json", plan.model_dump())
                    await emit(
                        EventLevel.INFO,
                        f"方案已生成（{response.planner}）：{plan.summary}",
                        data={"plan": plan.model_dump(), "planner": response.planner},
                    )
                    for warning in response.warnings:
                        await emit(EventLevel.WARNING, f"方案提醒：{warning}")

                plan = self._maybe_add_export(plan, request)
                record.plan = plan

                executor = PlanExecutor(
                    self.adapter,
                    self.settings,
                    emit,
                    run_dir=Path(record.run_dir) if record.run_dir else None,
                )
                result = await executor.run(
                    plan,
                    library=library,
                    dry_run=record.dry_run,
                    continue_on_error=record.continue_on_error,
                )

                record.timeline = result.timeline
                record.artifacts = result.artifacts
                record.error = "；".join(result.errors[:5]) or None
                record.status = JobStatus.SUCCEEDED if result.success else JobStatus.FAILED
                if result.timeline is not None:
                    self._write_json(record, "timeline.json", result.timeline.model_dump())
            except asyncio.CancelledError:
                record.status = JobStatus.CANCELLED
                record.error = "任务被取消"
                await emit(EventLevel.WARNING, "任务被取消")
                raise
            except Exception as exc:  # noqa: BLE001 - 任何失败都要落到任务状态上
                logger.exception("任务 %s 失败", record.id)
                record.status = JobStatus.FAILED
                record.error = str(exc)
                await emit(EventLevel.ERROR, f"任务失败：{exc}")
            finally:
                record.finished_at = utc_now()
                if record.status is JobStatus.SUCCEEDED:
                    record.progress = 1.0
                await emit(EventLevel.INFO, f"任务状态：{record.status.value}")
                self._persist(record)
                self._close_stream(record.id)

    def _maybe_add_export(self, plan: EditPlan, request: JobCreateRequest) -> EditPlan:
        if not request.auto_export:
            return plan
        if any(isinstance(op, ExportSequenceOp) for op in plan.operations):
            return plan
        plan.operations.append(
            ExportSequenceOp(
                op=OperationType.EXPORT_SEQUENCE,
                output=f"{plan.project_name}.mp4",
                format="H.264",
                note="请求里开启了 auto_export，自动追加一次导出",
            )
        )
        return plan

    async def _load_library(self, request: JobCreateRequest) -> MediaLibrary:
        if not request.media_root:
            return MediaLibrary(root="", scanned_at=utc_now(), assets=[])
        return await asyncio.to_thread(get_library, request.media_root, settings=self.settings)

    # ---------------------------------------------------------------- 事件流
    def subscribe(self, job_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._queues.setdefault(job_id, []).append(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        queues = self._queues.get(job_id)
        if queues and queue in queues:
            queues.remove(queue)
        if queues is not None and not queues:
            self._queues.pop(job_id, None)

    def _close_stream(self, job_id: str) -> None:
        for queue in list(self._queues.get(job_id, [])):
            queue.put_nowait(None)

    def _make_emitter(self, record: JobRecord):
        async def emit(
            level: EventLevel,
            message: str,
            *,
            op_index: int | None = None,
            op: str | None = None,
            data: dict | None = None,
        ) -> None:
            await self._emit(record, level, message, op_index=op_index, op=op, data=data)

        return emit

    async def _emit(
        self,
        record: JobRecord,
        level: EventLevel,
        message: str,
        *,
        op_index: int | None = None,
        op: str | None = None,
        data: dict | None = None,
    ) -> None:
        self._event_seq[record.id] = self._event_seq.get(record.id, 0) + 1
        event = JobEvent(
            seq=self._event_seq[record.id],
            level=level,
            message=message,
            op_index=op_index,
            op=op,
            data=data or {},
        )
        record.events.append(event)
        if len(record.events) > MAX_EVENTS_KEPT:
            record.events = record.events[-MAX_EVENTS_KEPT:]

        if op_index is not None and record.operation_count:
            record.progress = min(0.99, round((op_index + 1) / record.operation_count, 3))

        self._append_event(record, event)
        for queue in list(self._queues.get(record.id, [])):
            queue.put_nowait(event)

    # ---------------------------------------------------------------- 持久化
    def _persist(self, record: JobRecord) -> None:
        if not record.run_dir:
            return
        payload = record.model_dump(exclude={"events"})
        payload["event_count"] = len(record.events)
        self._write_json(record, "job.json", payload)

    def _write_json(self, record: JobRecord, filename: str, payload: dict) -> None:
        if not record.run_dir:
            return
        target = Path(record.run_dir) / filename
        try:
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
        except OSError as exc:  # pragma: no cover
            logger.warning("写入 %s 失败：%s", target, exc)

    def _append_event(self, record: JobRecord, event: JobEvent) -> None:
        if not record.run_dir:
            return
        target = Path(record.run_dir) / "events.jsonl"
        try:
            with target.open("a", encoding="utf-8") as handle:
                handle.write(event.model_dump_json() + "\n")
        except OSError as exc:  # pragma: no cover
            logger.debug("追加事件日志失败：%s", exc)

    # ---------------------------------------------------------------- 取消
    async def cancel(self, job_id: str) -> JobRecord:
        record = self.get_job(job_id)
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return record

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        for job_id in list(self._queues):
            self._close_stream(job_id)
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)
