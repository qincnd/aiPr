"""时间线快照模型。

不管底层是 Mock 还是真实的 Premiere Pro，执行完都统一产出这份结构，
方便前端展示、回归测试和后续二次剪辑。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class TimelineClip(BaseModel):
    id: str
    name: str
    media_ref: str
    track: str
    start: float = Field(description="时间线起点（秒）")
    end: float = Field(description="时间线终点（秒）")
    source_in: float = Field(description="源素材入点（秒）")
    source_out: float = Field(description="源素材出点（秒）")

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


class TimelineMarker(BaseModel):
    at: float
    name: str
    color: str = "green"
    duration: float = 0.0


class TimelineEffect(BaseModel):
    clip_id: str
    effect: str
    params: dict = Field(default_factory=dict)


class TimelineTransition(BaseModel):
    track: str
    at: float
    transition: str
    duration: float


class TimelineTrack(BaseModel):
    name: str
    kind: str = Field(description="video / audio")
    clips: list[TimelineClip] = Field(default_factory=list)

    @property
    def duration(self) -> float:
        return max((clip.end for clip in self.clips), default=0.0)


class TimelineState(BaseModel):
    sequence_name: str = "AI Sequence"
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    preset: str | None = None
    tracks: list[TimelineTrack] = Field(default_factory=list)
    markers: list[TimelineMarker] = Field(default_factory=list)
    effects: list[TimelineEffect] = Field(default_factory=list)
    transitions: list[TimelineTransition] = Field(default_factory=list)

    @property
    def duration(self) -> float:
        return max((track.duration for track in self.tracks), default=0.0)

    def track(self, name: str) -> TimelineTrack | None:
        return next((item for item in self.tracks if item.name == name), None)


class ClipHandle(BaseModel):
    """执行器内部使用的片段句柄。"""

    ref: str
    track: str
    media_ref: str
    start: float
    end: float
    source_in: float
    source_out: float
