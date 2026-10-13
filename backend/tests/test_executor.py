"""方案执行器测试：把一份 Edit Plan 完整跑一遍 Mock 适配器。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import PremiereError
from app.models.job import EventLevel
from app.models.media import MediaKind
from app.models.plan import EditPlan
from app.services.executor import PlanExecutor
from app.services.premiere.mock import MockPremiereAdapter
from tests.conftest import make_asset, make_library


class EventCollector:
    def __init__(self) -> None:
        self.events: list[tuple[EventLevel, str]] = []

    async def __call__(
        self,
        level: EventLevel,
        message: str,
        *,
        op_index: int | None = None,
        op: str | None = None,
        data: dict | None = None,
    ) -> None:
        self.events.append((level, message))

    def messages(self) -> str:
        return "\n".join(message for _, message in self.events)

    def errors(self) -> list[str]:
        return [message for level, message in self.events if level is EventLevel.ERROR]


def build_plan(media_ids: list[str]) -> EditPlan:
    operations = [
        {
            "op": "create_sequence",
            "name": "Executor Sequence",
            "width": 1920,
            "height": 1080,
            "fps": 30.0,
            "note": "建立序列",
        },
        {
            "op": "append_clip",
            "media": media_ids[0],
            "ref": "shot_1",
            "track": "V1",
            "in_point": 0.0,
            "out_point": 4.0,
        },
        {
            "op": "append_clip",
            "media": media_ids[1],
            "ref": "shot_2",
            "track": "V1",
            "in_point": 1.0,
            "out_point": 5.0,
        },
        {"op": "apply_transition", "at": 4.0, "track": "V1", "transition": "cross_dissolve", "duration": 1.0},
        {"op": "razor", "at": 6.0, "track": "V1"},
        {"op": "trim_clip", "clip": {"ref": "shot_1"}, "in_point": 0.5, "out_point": 3.0},
        {"op": "move_clip", "clip": {"track": "V1", "index": 0}, "to_time": 0.0},
        {"op": "apply_effect", "clip": {"ref": "shot_2"}, "effect": "Lumetri Color", "params": {"Exposure": 0.3}},
        {"op": "set_volume", "clip": {"ref": "shot_2"}, "db": -6.0},
        {"op": "add_marker", "at": 2.0, "name": "看点", "color": "red"},
        {"op": "export_sequence", "output": "executor_out.mp4", "format": "H.264"},
    ]
    return EditPlan.model_validate(
        {
            "version": "1.0",
            "project_name": "executor_test",
            "summary": "执行器测试方案",
            "sequence": {"name": "Executor Sequence", "width": 1920, "height": 1080, "fps": 30.0},
            "operations": operations,
        }
    )


@pytest.fixture
def setup(tmp_path: Path, media_dir: Path):
    assets = [
        make_asset(media_dir, "a.mp4", duration=10.0),
        make_asset(media_dir, "b.mp4", duration=10.0),
        make_asset(media_dir, "bgm.wav", duration=30.0, kind=MediaKind.AUDIO),
    ]
    library = make_library(media_dir, assets)
    adapter = MockPremiereAdapter(tmp_path / "mock")
    settings = Settings(
        media_roots=str(media_dir),
        workspace_dir=tmp_path / "workspace",
        llm_api_key=None,
    )
    collector = EventCollector()
    executor = PlanExecutor(adapter, settings, collector, run_dir=tmp_path / "run")
    return adapter, library, settings, collector, executor


async def test_executor_applies_every_operation(setup):
    adapter, library, _, collector, executor = setup
    plan = build_plan([library.assets[0].id, library.assets[1].id])

    result = await executor.run(plan, library=library)

    assert result.success is True, collector.errors()
    assert result.operations_done == result.operations_total == len(plan.operations)
    assert result.errors == []

    timeline = result.timeline
    assert timeline is not None
    assert timeline.sequence_name == "Executor Sequence"

    track = timeline.track("V1")
    assert len(track.clips) == 3  # razor 把第二个片段切成了两段
    assert track.clips[0].source_in == 0.5 and track.clips[0].source_out == 3.0
    assert timeline.markers[0].name == "看点"
    assert {effect.effect for effect in timeline.effects} == {"Lumetri Color", "Volume"}
    assert timeline.transitions[0].transition == "cross_dissolve"

    assert any(path.endswith(".edl") for path in result.artifacts)
    assert "执行结束" in collector.messages()


async def test_executor_reports_unknown_media_and_continues(setup):
    adapter, library, _, collector, executor = setup
    plan = build_plan(["不存在的素材.mp4", library.assets[1].id])

    result = await executor.run(plan, library=library, continue_on_error=True)

    assert result.success is False
    # append_clip 与随后依赖它的 trim_clip 各失败一次，其余操作照常执行
    assert len(result.errors) == 2
    assert result.operations_done + len(result.errors) == result.operations_total
    assert any("不存在的素材.mp4" in error for error in result.errors)
    assert result.timeline is not None  # 后续操作仍然执行了
    assert collector.errors()


async def test_executor_stops_on_first_error_when_configured(setup):
    _, library, _, _, executor = setup
    plan = build_plan(["不存在的素材.mp4", library.assets[1].id])

    result = await executor.run(plan, library=library, continue_on_error=False)

    assert result.success is False
    assert result.operations_done == 1  # 只完成了 create_sequence


async def test_create_sequence_failure_is_fatal_even_when_continuing(setup):
    """建序列失败必须停：否则后续 append_clip 会画到 Premiere 当前打开的无关序列上。"""

    _, library, _, collector, executor = setup
    plan = build_plan([library.assets[0].id, library.assets[1].id])
    plan.operations[0].enabled = True

    def boom(*args, **kwargs):
        # 注意：适配器方法是被 asyncio.to_thread 调用的，这里必须是同步函数
        raise PremiereError("模拟建序列失败")

    executor.adapter.create_sequence = boom  # type: ignore[method-assign]
    result = await executor.run(plan, library=library, continue_on_error=True)

    assert result.success is False
    assert result.operations_done == 0
    assert len(result.errors) == 1
    assert any("不再执行" in message for _, message in collector.events)


async def test_dry_run_skips_export(setup):
    _, library, _, collector, executor = setup
    plan = build_plan([library.assets[0].id, library.assets[1].id])

    result = await executor.run(plan, library=library, dry_run=True)

    assert result.artifacts == []
    assert "跳过导出" in collector.messages()


async def test_export_path_must_stay_inside_workspace(setup):
    _, library, _, _, executor = setup
    plan = build_plan([library.assets[0].id, library.assets[1].id])
    plan.operations[-1].output = "../escape.mp4"

    result = await executor.run(plan, library=library)

    assert result.success is False
    assert any("导出路径" in error for error in result.errors)


async def test_clip_reference_by_time_is_resolved(setup):
    _, library, _, collector, executor = setup
    plan = EditPlan.model_validate(
        {
            "project_name": "by_time_reference",
            "operations": [
                {"op": "create_sequence", "name": "S", "width": 1920, "height": 1080, "fps": 30.0},
                {
                    "op": "append_clip",
                    "media": library.assets[0].id,
                    "track": "V1",
                    "in_point": 0.0,
                    "out_point": 4.0,
                },
                {
                    "op": "append_clip",
                    "media": library.assets[1].id,
                    "track": "V1",
                    "in_point": 0.0,
                    "out_point": 4.0,
                },
                # 没给 ref，用「轨道 + 时间」直接定位第二个片段
                {"op": "set_volume", "clip": {"track": "V1", "at": 5.0}, "db": -3.0},
            ],
        }
    )

    result = await executor.run(plan, library=library)

    assert result.success is True, collector.errors()
    assert result.timeline.effects[0].effect == "Volume"
