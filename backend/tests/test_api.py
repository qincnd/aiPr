"""接口层端到端测试：走一遍前端会用的完整流程。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    (media / "shot_a.mp4").write_bytes(b"fake-a")
    (media / "shot_b.mp4").write_bytes(b"fake-b")
    (media / "shot_c.mov").write_bytes(b"fake-c")
    (media / "bgm.wav").write_bytes(b"fake-bgm")

    monkeypatch.setenv("AIPR_MEDIA_ROOTS", str(media))
    monkeypatch.setenv("AIPR_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("AIPR_PREMIERE_BACKEND", "mock")
    monkeypatch.setenv("AIPR_LLM_ENABLED", "false")
    monkeypatch.setenv("AIPR_LLM_API_KEY", "")
    get_settings.cache_clear()

    with TestClient(app) as test_client:
        yield test_client, media
    get_settings.cache_clear()


def wait_for_job(client: TestClient, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = client.get(f"/api/jobs/{job_id}").json()["job"]
        if detail["status"] in {"succeeded", "failed", "cancelled"}:
            return detail
        time.sleep(0.05)
    raise AssertionError(f"任务超时未结束：{detail}")


def test_health_and_system(client):
    client, media = client

    assert client.get("/api/health").json()["status"] == "ok"

    info = client.get("/api/system").json()
    assert info["premiere"]["backend"] == "mock"
    assert info["premiere"]["available"] is True
    assert info["llm_configured"] is False
    assert str(media) in info["media_roots"]


def test_media_roots_and_scan(client):
    client, media = client

    roots = client.get("/api/media/roots").json()
    assert str(media) in roots["roots"]

    payload = client.post("/api/media/scan", json={"path": str(media), "probe": False}).json()
    names = sorted(asset["name"] for asset in payload["library"]["assets"])
    assert names == ["bgm.wav", "shot_a.mp4", "shot_b.mp4", "shot_c.mov"]
    assert payload["library"]["totals"]["video"] == 3


def test_scan_rejects_path_outside_roots(client, tmp_path: Path):
    client, _ = client

    response = client.post("/api/media/scan", json={"path": str(tmp_path.parent), "probe": False})
    assert response.status_code == 403
    assert response.json()["code"] == "media_access_denied"


def test_plan_endpoint_uses_heuristic_without_api_key(client):
    client, media = client

    response = client.post(
        "/api/plan",
        json={
            "prompt": "剪成 12 秒的短片并导出",
            "media_root": str(media),
            "options": {"use_llm": True, "target_duration": 12},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["planner"] == "heuristic"
    assert body["plan"]["operations"][0]["op"] == "create_sequence"
    assert body["plan"]["operations"][-1]["op"] == "export_sequence"
    assert any("AIPR_LLM_API_KEY" in warning for warning in body["warnings"])


def test_job_runs_end_to_end_and_writes_run_artifacts(client):
    client, media = client

    created = client.post(
        "/api/jobs",
        json={"prompt": "把素材剪成 9 秒的成片并导出", "media_root": str(media)},
    )
    assert created.status_code == 202
    job_id = created.json()["job"]["id"]

    detail = wait_for_job(client, job_id)
    assert detail["status"] == "succeeded", detail
    assert detail["progress"] == 1.0
    assert detail["timeline"]["tracks"]

    events = client.get(f"/api/jobs/{job_id}/events").json()["events"]
    messages = " ".join(event["message"] for event in events)
    assert "导出" in messages
    assert "执行结束" in messages

    run_dir = Path(detail["run_dir"])
    assert (run_dir / "job.json").exists()
    assert (run_dir / "plan.json").exists()
    assert (run_dir / "timeline.json").exists()
    assert (run_dir / "events.jsonl").read_text(encoding="utf-8").strip()

    assert detail["artifacts"]


def test_job_accepts_explicit_plan(client):
    client, media = client

    created = client.post(
        "/api/jobs",
        json={
            "media_root": str(media),
            "plan": {
                "project_name": "manual_plan",
                "operations": [
                    {"op": "create_sequence", "name": "Manual", "width": 1280, "height": 720, "fps": 25.0},
                    {
                        "op": "append_clip",
                        "media": "shot_a.mp4",
                        "ref": "only",
                        "track": "V1",
                        "in_point": 0.0,
                        "out_point": 3.0,
                    },
                    {"op": "add_marker", "at": 1.0, "name": "标记"},
                ],
            },
        },
    )
    assert created.status_code == 202
    detail = wait_for_job(client, created.json()["job"]["id"])

    assert detail["status"] == "succeeded", detail
    assert detail["plan"]["project_name"] == "manual_plan"
    assert detail["timeline"]["markers"][0]["name"] == "标记"


def test_job_request_without_prompt_or_plan_is_rejected(client):
    client, _ = client

    response = client.post("/api/jobs", json={})
    assert response.status_code == 422


def test_unknown_job_returns_404(client):
    client, _ = client

    assert client.get("/api/jobs/does-not-exist").status_code == 404


def test_job_websocket_streams_until_done(client):
    client, media = client

    job_id = client.post(
        "/api/jobs",
        json={"prompt": "剪成 6 秒并导出", "media_root": str(media)},
    ).json()["job"]["id"]

    with client.websocket_connect(f"/api/ws/jobs/{job_id}") as websocket:
        kinds: list[str] = []
        for _ in range(500):
            message = websocket.receive_json()
            kinds.append(message["type"])
            if message["type"] == "done":
                break

    assert kinds[0] == "snapshot"
    assert "event" in kinds
    assert kinds[-1] == "done"


def test_websocket_closes_for_unknown_job(client):
    client, _ = client

    with pytest.raises(Exception):
        with client.websocket_connect("/api/ws/jobs/nope") as websocket:
            websocket.receive_json()
