"""素材库数据模型。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class MediaKind(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    PROJECT = "project"
    OTHER = "other"


VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".mxf", ".avi", ".mkv", ".mpg", ".mpeg",
    ".wmv", ".flv", ".webm", ".r3d", ".braw", ".mts", ".m2ts", ".vob",
}
AUDIO_EXTENSIONS = {
    ".wav", ".mp3", ".aac", ".m4a", ".flac", ".aif", ".aiff", ".ogg", ".wma", ".opus",
}
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp",
    ".psd", ".dng", ".cr2", ".cr3", ".arw", ".nef", ".heic",
}
PROJECT_EXTENSIONS = {".prproj", ".aep", ".mogrt", ".prtl", ".prel"}

ALL_EXTENSIONS = (
    VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | IMAGE_EXTENSIONS | PROJECT_EXTENSIONS
)


def classify_extension(extension: str) -> MediaKind:
    ext = extension.lower()
    if ext in VIDEO_EXTENSIONS:
        return MediaKind.VIDEO
    if ext in AUDIO_EXTENSIONS:
        return MediaKind.AUDIO
    if ext in IMAGE_EXTENSIONS:
        return MediaKind.IMAGE
    if ext in PROJECT_EXTENSIONS:
        return MediaKind.PROJECT
    return MediaKind.OTHER


class MediaAsset(BaseModel):
    """单个素材文件。"""

    id: str = Field(description="稳定的短哈希，方案里引用素材时优先用它")
    path: str = Field(description="绝对路径")
    name: str = Field(description="文件名（含扩展名）")
    stem: str = Field(description="文件名（不含扩展名）")
    relative_path: str = Field(description="相对素材根目录的路径")
    kind: MediaKind
    extension: str
    size_bytes: int
    modified_at: datetime | None = None

    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    probe_source: Literal["ffprobe", "metadata-only"] = "metadata-only"

    @staticmethod
    def make_id(path: Path) -> str:
        return hashlib.sha1(str(path).lower().encode("utf-8")).hexdigest()[:12]

    def descriptor(self) -> str:
        """给大模型看的一行摘要。"""

        parts = [f"id={self.id}", f"name={self.name}", f"kind={self.kind.value}"]
        if self.duration_seconds is not None:
            parts.append(f"duration={self.duration_seconds:.2f}s")
        if self.width and self.height:
            parts.append(f"resolution={self.width}x{self.height}")
        if self.fps:
            parts.append(f"fps={self.fps:g}")
        if self.sample_rate and self.kind is MediaKind.AUDIO:
            parts.append(f"sample_rate={self.sample_rate}")
        return " | ".join(parts)


class MediaLibrary(BaseModel):
    """一次素材扫描的结果。"""

    root: str
    scanned_at: datetime
    recursive: bool = True
    probed: bool = False
    assets: list[MediaAsset] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    totals: dict[str, int] = Field(default_factory=dict)

    def build_index(self) -> dict[str, MediaAsset]:
        """构建「引用键 -> 素材」的查找表，键统一小写。"""

        index: dict[str, MediaAsset] = {}
        for asset in self.assets:
            for key in (asset.id, asset.name, asset.stem, asset.relative_path, asset.path):
                index.setdefault(key.lower(), asset)
        return index

    def find(self, key: str) -> MediaAsset | None:
        return self.build_index().get(key.strip().lower())

    def by_kind(self, kind: MediaKind) -> list[MediaAsset]:
        return [asset for asset in self.assets if asset.kind is kind]

    def recompute_totals(self) -> "MediaLibrary":
        totals: dict[str, int] = {}
        for asset in self.assets:
            totals[asset.kind.value] = totals.get(asset.kind.value, 0) + 1
        self.totals = totals
        return self
