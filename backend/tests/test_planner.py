"""方案生成测试：启发式、大模型成功路径、大模型失败兜底。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import LLMError, PlanValidationError
from app.models.api import PlanOptions, PlanRequest
from app.models.media import MediaKind
from app.models.plan import (
    AppendClipOp,
    CreateSequenceOp,
    EditPlan,
    ExportSequenceOp,
    SetVolumeOp,
)
from app.services.llm.client import parse_json_object
from app.services.planner.heuristic_planner import HeuristicPlanner
from app.services.planner.service import PlannerService
from tests.conftest import make_asset, make_library


def build_library(media_dir: Path):
    assets = [make_asset(media_dir, f"clip_{index}.mp4", duration=12.0) for index in range(4)]
    assets.append(make_asset(media_dir, "bgm.wav", duration=90.0, kind=MediaKind.AUDIO))
    return make_library(media_dir, assets)


def build_request(prompt: str, **options) -> PlanRequest:
    return PlanRequest(
        prompt=prompt,
        media_root="ignored",
        options=PlanOptions(**options),
    )


async def test_heuristic_plan_follows_prompt_duration(media_dir: Path):
    library = build_library(media_dir)
    response = await HeuristicPlanner().create_plan(
        build_request("把素材剪成 20 秒的短片，配一段背景音乐，最后导出成片"), library
    )
    plan = response.plan

    assert plan.source == "heuristic"
    assert isinstance(plan.operations[0], CreateSequenceOp)
    assert isinstance(plan.operations[-1], ExportSequenceOp)

    video_clips = [
        op for op in plan.operations if isinstance(op, AppendClipOp) and op.track == "V1"
    ]
    assert len(video_clips) == 4
    total = sum((op.out_point or 0) - op.in_point for op in video_clips)
    assert total == pytest.approx(20.0, abs=0.2)
    assert all(op.ref for op in video_clips)

    assert any(op.track == "A1" for op in plan.operations if isinstance(op, AppendClipOp))
    assert any(isinstance(op, SetVolumeOp) for op in plan.operations)


async def test_heuristic_plan_without_music_or_export_keywords(media_dir: Path):
    library = build_library(media_dir)
    response = await HeuristicPlanner().create_plan(build_request("随便剪一个 15 秒的片子"), library)

    assert not any(isinstance(op, ExportSequenceOp) for op in response.plan.operations)
    assert not any(
        isinstance(op, AppendClipOp) and op.track.startswith("A")
        for op in response.plan.operations
    )


async def test_heuristic_plan_requires_video(media_dir: Path):
    library = make_library(
        media_dir, [make_asset(media_dir, "bgm.wav", kind=MediaKind.AUDIO, duration=30.0)]
    )
    with pytest.raises(PlanValidationError):
        await HeuristicPlanner().create_plan(build_request("剪个片子"), library)


async def test_heuristic_respects_max_clips_and_sampling(media_dir: Path):
    library = build_library(media_dir)
    response = await HeuristicPlanner().create_plan(
        build_request("剪 30 秒", max_clips=2), library
    )
    clips = [op for op in response.plan.operations if isinstance(op, AppendClipOp)]
    assert len(clips) == 2


class _StubClient:
    """可控的假大模型客户端。"""

    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls = 0
        self.configured = True

    async def complete_json(self, **_: object):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


def _valid_plan_payload(media_id: str, extra_media: str | None = None) -> dict:
    operations = [
        {
            "op": "create_sequence",
            "name": "LLM Sequence",
            "width": 1920,
            "height": 1080,
            "fps": 25.0,
        },
        {
            "op": "append_clip",
            "media": media_id,
            "ref": "opening",
            "track": "V1",
            "in_point": 0.0,
            "out_point": 4.0,
            "note": "开场镜头",
        },
        {
            "op": "apply_transition",
            "at": 4.0,
            "track": "V1",
            "transition": "cross_dissolve",
            "duration": 0.8,
        },
    ]
    if extra_media:
        operations.append(
            {"op": "append_clip", "media": extra_media, "track": "V1", "in_point": 0.0, "out_point": 3.0}
        )
    return {
        "version": "1.0",
        "project_name": "llm_project",
        "summary": "大模型生成的方案",
        "sequence": {"name": "LLM Sequence", "width": 1920, "height": 1080, "fps": 25.0},
        "operations": operations,
    }


async def test_llm_planner_returns_valid_plan(media_dir: Path):
    library = build_library(media_dir)
    payload = _valid_plan_payload(library.assets[0].id)
    service = PlannerService(Settings(llm_api_key="test-key", media_roots=str(media_dir)))
    service.client = _StubClient(payload=payload)

    response = await service.create_plan(build_request("生成一个开场"), library)

    assert response.planner == "llm"
    assert response.plan.project_name == "llm_project"
    assert response.plan.source == "manual"  # 模型没写 source，保持默认，不影响执行
    assert response.warnings == []


async def test_llm_planner_warns_on_unknown_media(media_dir: Path):
    library = build_library(media_dir)
    payload = _valid_plan_payload(library.assets[0].id, extra_media="不存在的素材.mp4")
    service = PlannerService(Settings(llm_api_key="test-key", media_roots=str(media_dir)))
    service.client = _StubClient(payload=payload)

    response = await service.create_plan(build_request("生成一个开场"), library)

    assert response.planner == "llm"
    assert any("不存在的素材.mp4" in warning for warning in response.warnings)


async def test_llm_planner_retries_with_schema_after_invalid_payload(media_dir: Path):
    library = build_library(media_dir)
    broken = {"project_name": "x", "operations": [{"op": "不存在的操作"}]}
    service = PlannerService(Settings(llm_api_key="test-key", media_roots=str(media_dir)))
    service.client = _StubClient(payload=broken)

    with pytest.raises(PlanValidationError):
        await service.create_plan(build_request("剪一下"), library, allow_fallback=False)
    assert service.client.calls == 2  # 第一次失败后带着错误信息重试了一次


async def test_service_falls_back_to_heuristic_without_api_key(media_dir: Path):
    library = build_library(media_dir)
    service = PlannerService(Settings(llm_api_key=None, media_roots=str(media_dir)))

    response = await service.create_plan(build_request("剪成 10 秒"), library)

    assert response.planner == "heuristic"
    assert any("AIPR_LLM_API_KEY" in warning for warning in response.warnings)


async def test_service_falls_back_when_llm_errors(media_dir: Path):
    library = build_library(media_dir)
    service = PlannerService(Settings(llm_api_key="test-key", media_roots=str(media_dir)))
    service.client = _StubClient(error=LLMError("网络超时"))

    response = await service.create_plan(build_request("剪成 10 秒"), library)

    assert response.planner == "heuristic"
    assert any("大模型方案不可用" in warning for warning in response.warnings)


async def test_service_uses_heuristic_when_llm_disabled(media_dir: Path):
    library = build_library(media_dir)
    service = PlannerService(Settings(llm_api_key="test-key", media_roots=str(media_dir)))

    response = await service.create_plan(build_request("剪成 10 秒", use_llm=False), library)

    assert response.planner == "heuristic"
    assert response.warnings == []


def test_parse_json_object_handles_markdown_fences_and_noise():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('好的，方案如下：{"a": {"b": 2}} 以上') == {"a": {"b": 2}}
    with pytest.raises(LLMError):
        parse_json_object("完全不是 JSON")


def test_edit_plan_schema_is_serialisable():
    schema = json.dumps(EditPlan.model_json_schema())
    assert "operations" in schema
