"""Mock 后端：在内存里模拟一条 Premiere 时间线。

存在的意义：
* 本机没装 Premiere Pro 也能跑通「提示词 -> 方案 -> 执行 -> 产物」的完整链路；
* 给方案执行器提供单元测试的确定性环境；
* 导出时额外产出 CMX3600 EDL，可以直接导入 Premiere Pro 验证排片结果。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.core.errors import PremiereError
from app.core.logging import get_logger
from app.models.media import MediaAsset
from app.models.plan import SequenceSpec
from app.models.timeline import (
    ClipHandle,
    TimelineClip,
    TimelineEffect,
    TimelineMarker,
    TimelineState,
    TimelineTrack,
    TimelineTransition,
)

from .base import PremiereAdapter, PremiereStatus

logger = get_logger(__name__)

EPSILON = 1e-6
FALLBACK_CLIP_DURATION = 5.0
DEFAULT_TRACKS = ("V1", "V2", "A1", "A2")


def track_kind(track_name: str) -> str:
    return "video" if track_name.upper().startswith("V") else "audio"


class MockPremiereAdapter(PremiereAdapter):
    name = "mock"
    description = "内置模拟后端：内存时间线，无需安装 Premiere Pro"

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._timeline: TimelineState | None = None
        self._imported: dict[str, MediaAsset] = {}
        self._bin_name: str | None = None
        self._project_path: Path | None = None
        self._saved_path: Path | None = None

    # ---------------------------------------------------------------- 生命周期
    def status(self) -> PremiereStatus:
        return PremiereStatus(
            backend=self.name,
            available=True,
            connected=True,
            message="Mock 后端已就绪：只模拟时间线，不会启动 Premiere Pro",
            project_name=self._timeline.sequence_name if self._timeline else None,
            version="mock-1.0",
            capabilities=self.capabilities(),
        )

    def open_project(self, path: Path | None) -> None:
        self._project_path = Path(path) if path else None
        self._timeline = None
        self._imported.clear()
        logger.info("Mock：打开工程 %s", self._project_path or "<新建>")

    def save(self) -> Path | None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "project_path": str(self._project_path) if self._project_path else None,
            "imported_media": [asset.path for asset in self._imported.values()],
            "sequence": self._timeline.model_dump() if self._timeline else None,
        }
        self._saved_path = self.work_dir / "mock_project.json"
        self._saved_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return self._saved_path

    def close(self) -> None:
        self._timeline = None

    def capabilities(self) -> dict[str, str]:
        return {
            "import_media": "simulated",
            "create_sequence": "simulated",
            "append_clip": "simulated",
            "insert_clip": "simulated",
            "move_clip": "simulated",
            "trim_clip": "simulated",
            "razor": "simulated",
            "delete_range": "simulated",
            "apply_transition": "simulated",
            "apply_effect": "simulated",
            "set_volume": "simulated",
            "add_marker": "simulated",
            "export_sequence": "simulated",
        }

    # ---------------------------------------------------------------- 素材
    def import_media(self, assets: list[MediaAsset], bin_name: str | None = None) -> list[str]:
        self._bin_name = bin_name or self._bin_name
        names: list[str] = []
        for asset in assets:
            self._imported[asset.id] = asset
            names.append(asset.name)
        logger.debug("Mock：导入 %d 个素材到 %s", len(names), self._bin_name or "根素材箱")
        return names

    # ---------------------------------------------------------------- 序列
    def create_sequence(
        self,
        spec: SequenceSpec,
        reference_assets: list[MediaAsset] | None = None,
    ) -> TimelineState:
        _ = reference_assets  # Mock 后端按 spec 直接建序列，不需要参照素材
        self._timeline = TimelineState(
            sequence_name=spec.name,
            width=spec.width,
            height=spec.height,
            fps=spec.fps or 30.0,
            preset=spec.preset,
            tracks=[TimelineTrack(name=name, kind=track_kind(name)) for name in DEFAULT_TRACKS],
        )
        return self._timeline

    # ---------------------------------------------------------------- 时间线编辑
    def append_clip(
        self,
        asset: MediaAsset,
        *,
        ref: str,
        track: str,
        in_point: float,
        out_point: float | None,
    ) -> ClipHandle:
        target = self._ensure_track(track)
        duration = self._clip_duration(asset, in_point, out_point)
        start = round(max((clip.end for clip in target.clips), default=0.0), 3)
        clip = TimelineClip(
            id=self._unique_clip_id(self._timeline, ref or asset.stem),
            name=asset.name,
            media_ref=asset.id,
            track=target.name,
            start=start,
            end=round(start + duration, 3),
            source_in=round(max(0.0, in_point), 3),
            source_out=round(max(0.0, in_point) + duration, 3),
        )
        target.clips.append(clip)
        target.clips.sort(key=lambda item: item.start)
        self._imported.setdefault(asset.id, asset)
        return self._to_handle(clip)

    def insert_clip(
        self,
        asset: MediaAsset,
        *,
        ref: str,
        track: str,
        at: float,
        in_point: float,
        out_point: float | None,
    ) -> ClipHandle:
        target = self._ensure_track(track)
        duration = self._clip_duration(asset, in_point, out_point)
        # 与真实 NLE 一致：插入点若落在片段内部，先切开再把后面的内容整体右移
        self.razor(at=at, track=target.name)
        for existing in target.clips:
            if existing.start >= at - EPSILON:
                existing.start = round(existing.start + duration, 3)
                existing.end = round(existing.end + duration, 3)

        clip = TimelineClip(
            id=self._unique_clip_id(self._timeline, ref or asset.stem),
            name=asset.name,
            media_ref=asset.id,
            track=target.name,
            start=round(at, 3),
            end=round(at + duration, 3),
            source_in=round(max(0.0, in_point), 3),
            source_out=round(max(0.0, in_point) + duration, 3),
        )
        target.clips.append(clip)
        target.clips.sort(key=lambda item: item.start)
        self._imported.setdefault(asset.id, asset)
        return self._to_handle(clip)

    def move_clip(self, handle: ClipHandle, *, to_time: float, track: str | None) -> ClipHandle:
        clip = self._find_clip(handle)
        duration = round(clip.end - clip.start, 3)
        if track and track != clip.track:
            self._track(clip.track).clips.remove(clip)
            clip.track = track
            self._ensure_track(track).clips.append(clip)
        clip.start = round(to_time, 3)
        clip.end = round(clip.start + duration, 3)
        self._sort_all_tracks()
        return self._to_handle(clip)

    def trim_clip(self, handle: ClipHandle, *, in_point: float, out_point: float | None) -> ClipHandle:
        clip = self._find_clip(handle)
        asset = self._imported.get(clip.media_ref)
        duration = self._clip_duration(asset, in_point, out_point, clip_name=clip.name)
        clip.source_in = round(max(0.0, in_point), 3)
        clip.source_out = round(max(0.0, in_point) + duration, 3)
        clip.end = round(clip.start + duration, 3)
        return self._to_handle(clip)

    def razor(self, *, at: float, track: str | None) -> None:
        targets = [self._track(track)] if track else list(self._timeline_required().tracks)
        for target in targets:
            updated: list[TimelineClip] = []
            for clip in target.clips:
                if clip.start + EPSILON < at < clip.end - EPSILON:
                    offset = round(at - clip.start, 3)
                    right = clip.model_copy(
                        update={
                            "id": self._unique_clip_id(self._timeline, f"{clip.id}_b"),
                            "start": round(at, 3),
                            "source_in": round(clip.source_in + offset, 3),
                        }
                    )
                    left = clip.model_copy(
                        update={
                            "end": round(at, 3),
                            "source_out": round(clip.source_in + offset, 3),
                        }
                    )
                    updated.extend([left, right])
                else:
                    updated.append(clip)
            target.clips = sorted(updated, key=lambda item: item.start)

    def delete_range(self, *, start: float, end: float, track: str | None, ripple: bool) -> None:
        if end <= start:
            raise PremiereError(f"删除区间非法：start={start} end={end}")
        span = end - start
        targets = [self._track(track)] if track else list(self._timeline_required().tracks)

        for target in targets:
            kept: list[TimelineClip] = []
            for clip in target.clips:
                if clip.end <= start + EPSILON or clip.start >= end - EPSILON:
                    kept.append(clip)
                    continue

                # 区间左边的部分（片段头部越界时保留）
                if clip.start < start:
                    kept.append(
                        clip.model_copy(
                            update={
                                "end": round(start, 3),
                                "source_out": round(clip.source_in + (start - clip.start), 3),
                            }
                        )
                    )

                # 区间右边的部分（源内容从 end 处接着播，长度是剩下的那一段）
                if clip.end > end:
                    remaining = round(clip.end - end, 3)
                    new_start = start if ripple else end
                    is_split = clip.start < start
                    kept.append(
                        clip.model_copy(
                            update={
                                "id": (
                                    self._unique_clip_id(self._timeline, f"{clip.id}_b")
                                    if is_split
                                    else clip.id
                                ),
                                "start": round(new_start, 3),
                                "end": round(new_start + remaining, 3),
                                "source_in": round(clip.source_in + (end - clip.start), 3),
                            }
                        )
                    )
                # 完全被区间覆盖的片段两者都不保留，直接丢弃

            if ripple:
                for clip in kept:
                    if clip.start >= end - EPSILON:
                        clip.start = round(clip.start - span, 3)
                        clip.end = round(clip.end - span, 3)
            target.clips = sorted(kept, key=lambda item: item.start)

    def apply_transition(
        self, *, at: float, track: str, transition: str, duration: float
    ) -> None:
        timeline = self._timeline_required()
        if not any(abs(clip.end - at) < 0.5 or abs(clip.start - at) < 0.5 for clip in self._track(track).clips):
            logger.warning("Mock：%s 轨在 %.2fs 处没有相邻片段，转场可能不生效", track, at)
        timeline.transitions.append(
            TimelineTransition(
                track=track, at=round(at, 3), transition=transition, duration=round(duration, 3)
            )
        )

    def apply_effect(self, handle: ClipHandle, *, effect: str, params: dict) -> None:
        clip = self._find_clip(handle)
        self._timeline_required().effects.append(
            TimelineEffect(clip_id=clip.id, effect=effect, params=dict(params))
        )

    def set_volume(self, handle: ClipHandle, *, db: float) -> None:
        self.apply_effect(handle, effect="Volume", params={"level_db": db})

    def add_marker(self, *, at: float, name: str, color: str, duration: float) -> None:
        timeline = self._timeline_required()
        timeline.markers.append(
            TimelineMarker(at=round(at, 3), name=name, color=color, duration=round(duration, 3))
        )
        timeline.markers.sort(key=lambda item: item.at)

    # ---------------------------------------------------------------- 导出
    def export_sequence(self, *, output: Path, preset: str | None, format: str) -> list[Path]:
        timeline = self._timeline_required()
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)

        snapshot = output.with_suffix(f"{output.suffix}.timeline.json")
        snapshot.write_text(timeline.model_dump_json(indent=2), encoding="utf-8")

        edl = output.with_suffix(".edl")
        edl.write_text(self.render_edl(timeline), encoding="utf-8")

        logger.info(
            "Mock：不渲染真实视频，已产出时间线快照与 EDL（%s / %s）", snapshot.name, edl.name
        )
        return [snapshot, edl]

    def snapshot(self) -> TimelineState | None:
        return self._timeline

    # ---------------------------------------------------------------- 内部工具
    def _timeline_required(self) -> TimelineState:
        if self._timeline is None:
            raise PremiereError("还没有创建序列，请先执行 create_sequence")
        return self._timeline

    def _track(self, name: str) -> TimelineTrack:
        timeline = self._timeline_required()
        found = timeline.track(name)
        if found is None:
            raise PremiereError(f"轨道不存在：{name}")
        return found

    def _ensure_track(self, name: str) -> TimelineTrack:
        timeline = self._timeline_required()
        found = timeline.track(name)
        if found is None:
            found = TimelineTrack(name=name, kind=track_kind(name))
            timeline.tracks.append(found)
        return found

    def _sort_all_tracks(self) -> None:
        for track in self._timeline_required().tracks:
            track.clips.sort(key=lambda item: item.start)

    def _find_clip(self, handle: ClipHandle) -> TimelineClip:
        timeline = self._timeline_required()
        for track in timeline.tracks:
            for clip in track.clips:
                if clip.id == handle.ref:
                    return clip
        raise PremiereError(f"时间线上找不到片段：{handle.ref}")

    @staticmethod
    def _unique_clip_id(timeline: TimelineState | None, base: str) -> str:
        slug = re.sub(r"[^0-9A-Za-z_]+", "_", base).strip("_") or "clip"
        existing = {
            clip.id for track in (timeline.tracks if timeline else []) for clip in track.clips
        }
        if slug not in existing:
            return slug
        counter = 2
        while f"{slug}_{counter}" in existing:
            counter += 1
        return f"{slug}_{counter}"

    def _clip_duration(
        self,
        asset: MediaAsset | None,
        in_point: float,
        out_point: float | None,
        *,
        clip_name: str = "",
    ) -> float:
        if out_point is not None:
            duration = out_point - in_point
        else:
            total = asset.duration_seconds if asset else None
            duration = (total - in_point) if total is not None else FALLBACK_CLIP_DURATION
        if duration <= 0.05:
            label = asset.name if asset else clip_name
            raise PremiereError(
                f"片段时长非法（{label}）：in_point={in_point} out_point={out_point}"
            )
        return round(duration, 3)

    @staticmethod
    def _to_handle(clip: TimelineClip) -> ClipHandle:
        return ClipHandle(
            ref=clip.id,
            track=clip.track,
            media_ref=clip.media_ref,
            start=clip.start,
            end=clip.end,
            source_in=clip.source_in,
            source_out=clip.source_out,
        )

    @staticmethod
    def _timecode(seconds: float, fps: float) -> str:
        fps_int = max(1, int(round(fps)))
        total_frames = int(round(max(0.0, seconds) * fps_int))
        frames = total_frames % fps_int
        total_seconds = total_frames // fps_int
        return (
            f"{total_seconds // 3600:02d}:"
            f"{(total_seconds // 60) % 60:02d}:"
            f"{total_seconds % 60:02d}:{frames:02d}"
        )

    @classmethod
    def render_edl(cls, timeline: TimelineState) -> str:
        """把时间线导出成 CMX3600 EDL（Premiere Pro 可直接导入）。"""

        lines = [f"TITLE: {timeline.sequence_name}", "FCM: NON-DROP FRAME", ""]
        events: list[tuple[float, str, TimelineClip]] = []
        for track in timeline.tracks:
            channel = "V" if track.kind == "video" else "A"
            for clip in track.clips:
                events.append((clip.start, channel, clip))
        events.sort(key=lambda item: (item[0], item[1]))

        for index, (_, channel, clip) in enumerate(events, start=1):
            reel = re.sub(r"[^A-Za-z0-9]", "", Path(clip.name).stem)[:8].upper() or "AX"
            lines.append(
                f"{index:03d}  {reel:<8} {channel:<4} C        "
                f"{cls._timecode(clip.source_in, timeline.fps)} "
                f"{cls._timecode(clip.source_out, timeline.fps)} "
                f"{cls._timecode(clip.start, timeline.fps)} "
                f"{cls._timecode(clip.end, timeline.fps)}"
            )
        return "\n".join(lines) + "\n"
