"""真实适配器的「读时间线」映射测试：用假的 Premiere 对象验证字段映射。

不需要装 Premiere，也不连网：只验证 pymiere 适配器把原生对象翻译成时间线快照时的行为，
尤其是 PR 2026 上「转场挂在轨道上、片段的 transitions 是 undefined」这条真机结论。
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.models.timeline import TimelineTransition


class FakeTime:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds


class FakeCollection:
    """模仿真机的集合：只暴露 numItems/numTracks 和下标访问。"""

    def __init__(self, items: list) -> None:
        self.numItems = len(items)
        self.numTracks = len(items)
        self._items = items

    def __getitem__(self, index: int):
        return self._items[index]


class FakeTransition:
    """真机上转场对象带 name / start(Time) / duration(Time)。"""

    def __init__(self, name: str, start: float, duration: float) -> None:
        self.name = name
        self.start = FakeTime(start)
        self.duration = FakeTime(duration)


class FakeClip:
    def __init__(self, name: str, start: float, end: float, in_point: float, out_point: float) -> None:
        self.name = name
        self.start = FakeTime(start)
        self.end = FakeTime(end)
        self.inPoint = FakeTime(in_point)
        self.outPoint = FakeTime(out_point)


class FakeTrack:
    def __init__(self, clips: list[FakeClip], transitions: list[FakeTransition] | None = None) -> None:
        self._clips = FakeCollection(clips)
        if transitions is not None:
            self.transitions = FakeCollection(transitions)

    @property
    def clips(self) -> FakeCollection:
        return self._clips


class FakeSequence:
    name = "Fake Sequence"

    def __init__(self) -> None:
        self.videoTracks = FakeCollection(
            [
                FakeTrack(
                    [FakeClip("a.mp4", 0.0, 2.0, 0.0, 2.0), FakeClip("b.mp4", 2.0, 4.0, 0.0, 2.0)],
                    [FakeTransition("Cross Dissolve", 2.0, 0.43)],
                ),
                FakeTrack([]),  # 没有 transitions 属性的轨道（旧版本/空轨道）
            ]
        )
        self.audioTracks = FakeCollection([FakeTrack([])])


def make_adapter(tmp_path: Path):
    from app.services.premiere.pymiere_adapter import PymierePremiereAdapter

    settings = Settings(
        media_roots=str(tmp_path),
        workspace_dir=str(tmp_path / "workspace"),
        premiere_backend="pymiere",
        llm_api_key=None,
    )
    adapter = PymierePremiereAdapter(settings)
    adapter._sequence = FakeSequence()  # type: ignore[assignment]
    adapter._sequence_settings = lambda sequence: {"width": 1280.0, "height": 720.0, "fps": 29.97}
    return adapter


def test_read_timeline_maps_track_transitions(tmp_path: Path):
    timeline = make_adapter(tmp_path)._read_timeline()

    assert timeline is not None
    assert (timeline.width, timeline.height) == (1280, 720)
    assert timeline.transitions == [
        TimelineTransition(track="V1", at=2.0, transition="Cross Dissolve", duration=0.43)
    ]
    assert len(timeline.track("V1").clips) == 2
    assert timeline.track("V2").clips == []


def test_read_timeline_survives_missing_transitions_attribute(tmp_path: Path):
    """轨道上没有 transitions 属性时要当作没有转场，而不是让整个快照失败。"""

    timeline = make_adapter(tmp_path)._read_timeline()

    assert timeline is not None
    assert len(timeline.tracks) == 3
    assert all(item.track == "V1" for item in timeline.transitions)
