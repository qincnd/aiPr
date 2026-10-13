"""Mock 适配器的时间线行为测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import PremiereError
from app.models.media import MediaKind
from app.models.plan import SequenceSpec
from tests.conftest import make_asset

SPEC = SequenceSpec(name="Test Sequence", width=1920, height=1080, fps=30.0)


@pytest.fixture
def adapter(tmp_path: Path):
    from app.services.premiere.mock import MockPremiereAdapter

    return MockPremiereAdapter(tmp_path / "mock")


def test_requires_sequence_before_editing(adapter, media_dir):
    asset = make_asset(media_dir, "a.mp4")
    with pytest.raises(PremiereError):
        adapter.append_clip(asset, ref="", track="V1", in_point=0.0, out_point=5.0)


def test_append_clips_builds_sequential_timeline(adapter, media_dir):
    adapter.create_sequence(SPEC)
    first = make_asset(media_dir, "a.mp4", duration=10.0)
    second = make_asset(media_dir, "b.mp4", duration=10.0)

    handle_a = adapter.append_clip(first, ref="a", track="V1", in_point=0.0, out_point=5.0)
    handle_b = adapter.append_clip(second, ref="b", track="V1", in_point=1.0, out_point=4.0)

    timeline = adapter.snapshot()
    track = timeline.track("V1")
    assert len(track.clips) == 2
    assert (handle_a.start, handle_a.end) == (0.0, 5.0)
    assert (handle_b.start, handle_b.end) == (5.0, 8.0)
    assert timeline.duration == 8.0


def test_insert_clip_shifts_following_clips(adapter, media_dir):
    adapter.create_sequence(SPEC)
    first = make_asset(media_dir, "a.mp4")
    middle = make_asset(media_dir, "b.mp4")
    adapter.append_clip(first, ref="a", track="V1", in_point=0.0, out_point=4.0)
    adapter.append_clip(first, ref="b", track="V1", in_point=0.0, out_point=4.0)

    adapter.insert_clip(middle, ref="mid", track="V1", at=2.0, in_point=0.0, out_point=2.0)

    # 插入点落在第一个片段内部：该片段被切开，插入点之后的内容整体右移 2s
    track = adapter.snapshot().track("V1")
    starts = [round(clip.start, 2) for clip in sorted(track.clips, key=lambda item: item.start)]
    assert starts == [0.0, 2.0, 4.0, 6.0]
    # 插入让整条时间线变长：4 + 4（原素材）+ 2（插入）= 10s
    assert adapter.snapshot().duration == 10.0


def test_razor_splits_clip_and_keeps_source_mapping(adapter, media_dir):
    adapter.create_sequence(SPEC)
    asset = make_asset(media_dir, "a.mp4")
    adapter.append_clip(asset, ref="a", track="V1", in_point=2.0, out_point=12.0)

    adapter.razor(at=7.0, track="V1")

    clips = sorted(adapter.snapshot().track("V1").clips, key=lambda item: item.start)
    assert len(clips) == 2
    assert (clips[0].start, clips[0].end) == (0.0, 7.0)
    assert clips[0].source_in == 2.0
    assert (clips[1].start, clips[1].end) == (7.0, 10.0)
    assert clips[1].source_in == 9.0


def test_delete_range_with_ripple_closes_gap(adapter, media_dir):
    adapter.create_sequence(SPEC)
    asset = make_asset(media_dir, "a.mp4")
    for index in range(3):
        adapter.append_clip(asset, ref=f"c{index}", track="V1", in_point=0.0, out_point=5.0)

    adapter.delete_range(start=5.0, end=8.0, track="V1", ripple=True)

    # 第二段被砍掉前 3 秒后只剩 2 秒，并吸附到 5s 处；第三段左移 3 秒
    track = adapter.snapshot().track("V1")
    assert len(track.clips) == 3
    assert adapter.snapshot().duration == 12.0
    assert sorted((clip.start, clip.end) for clip in track.clips) == [
        (0.0, 5.0),
        (5.0, 7.0),
        (7.0, 12.0),
    ]


def test_delete_range_without_ripple_keeps_gap(adapter, media_dir):
    adapter.create_sequence(SPEC)
    asset = make_asset(media_dir, "a.mp4")
    for index in range(3):
        adapter.append_clip(asset, ref=f"c{index}", track="V1", in_point=0.0, out_point=5.0)

    adapter.delete_range(start=5.0, end=8.0, track="V1", ripple=False)

    # 不吸附：第二段剩下的 2 秒留在 8s 处，中间留下 3 秒空隙
    clips = sorted(adapter.snapshot().track("V1").clips, key=lambda item: item.start)
    assert [(clip.start, clip.end) for clip in clips] == [(0.0, 5.0), (8.0, 10.0), (10.0, 15.0)]


def test_trim_and_move_clip(adapter, media_dir):
    adapter.create_sequence(SPEC)
    asset = make_asset(media_dir, "a.mp4", duration=20.0)
    handle = adapter.append_clip(asset, ref="a", track="V1", in_point=0.0, out_point=10.0)

    trimmed = adapter.trim_clip(handle, in_point=1.0, out_point=4.0)
    assert (trimmed.source_in, trimmed.source_out) == (1.0, 4.0)
    assert (trimmed.start, trimmed.end) == (0.0, 3.0)

    moved = adapter.move_clip(trimmed, to_time=6.0, track=None)
    assert (moved.start, moved.end) == (6.0, 9.0)


def test_markers_effects_transitions_and_volume(adapter, media_dir):
    adapter.create_sequence(SPEC)
    asset = make_asset(media_dir, "a.mp4")
    handle = adapter.append_clip(asset, ref="a", track="V1", in_point=0.0, out_point=4.0)

    adapter.apply_transition(at=4.0, track="V1", transition="cross_dissolve", duration=1.0)
    adapter.apply_effect(handle, effect="Lumetri Color", params={"Exposure": 0.5})
    adapter.set_volume(handle, db=-6.0)
    adapter.add_marker(at=1.0, name="看点", color="red", duration=0.0)

    timeline = adapter.snapshot()
    assert timeline.transitions[0].transition == "cross_dissolve"
    assert {effect.effect for effect in timeline.effects} == {"Lumetri Color", "Volume"}
    assert timeline.markers[0].name == "看点"


def test_export_writes_timeline_snapshot_and_edl(adapter, media_dir, tmp_path):
    adapter.create_sequence(SPEC)
    asset = make_asset(media_dir, "scene_one.mp4")
    adapter.append_clip(asset, ref="a", track="V1", in_point=0.0, out_point=5.0)
    adapter.append_clip(asset, ref="b", track="V1", in_point=0.0, out_point=5.0)

    output = tmp_path / "exports" / "out.mp4"
    artifacts = adapter.export_sequence(output=output, preset=None, format="H.264")

    names = sorted(path.name for path in artifacts)
    assert names == ["out.edl", "out.mp4.timeline.json"]
    edl = (tmp_path / "exports" / "out.edl").read_text(encoding="utf-8")
    assert edl.startswith("TITLE: Test Sequence")
    assert edl.count("C        ") == 2
    assert "00:00:05:00" in edl


def test_import_media_deduplicates_and_reports_names(adapter, media_dir):
    assets = [make_asset(media_dir, "a.mp4"), make_asset(media_dir, "b.mp4")]
    names = adapter.import_media(assets, "素材箱")
    assert names == ["a.mp4", "b.mp4"]
    assert adapter.import_media(assets) == ["a.mp4", "b.mp4"]


def test_audio_track_kind_is_detected(adapter, media_dir):
    adapter.create_sequence(SPEC)
    music = make_asset(media_dir, "bgm.wav", kind=MediaKind.AUDIO)
    adapter.append_clip(music, ref="bgm", track="A1", in_point=0.0, out_point=3.0)
    track = adapter.snapshot().track("A1")
    assert track.kind == "audio"
    assert track.clips[0].track == "A1"
