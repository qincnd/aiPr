"""测试公共夹具。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models.media import MediaAsset, MediaKind, MediaLibrary


def make_asset(
    directory: Path,
    name: str,
    *,
    duration: float | None = 10.0,
    kind: MediaKind | None = None,
) -> MediaAsset:
    """在临时目录里造一个假素材文件并返回对应的 MediaAsset。"""

    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake-media-bytes")
    resolved_kind = kind or (
        MediaKind.AUDIO if path.suffix.lower() in {".wav", ".mp3"} else MediaKind.VIDEO
    )
    return MediaAsset(
        id=MediaAsset.make_id(path),
        path=str(path),
        name=path.name,
        stem=path.stem,
        relative_path=path.name,
        kind=resolved_kind,
        extension=path.suffix.lower(),
        size_bytes=path.stat().st_size,
        modified_at=datetime.now(timezone.utc),
        duration_seconds=duration,
        width=1920 if resolved_kind is MediaKind.VIDEO else None,
        height=1080 if resolved_kind is MediaKind.VIDEO else None,
        fps=30.0 if resolved_kind is MediaKind.VIDEO else None,
        probe_source="ffprobe" if duration else "metadata-only",
    )


def make_library(directory: Path, assets: list[MediaAsset]) -> MediaLibrary:
    library = MediaLibrary(
        root=str(directory),
        scanned_at=datetime.now(timezone.utc),
        probed=all(asset.probe_source == "ffprobe" for asset in assets),
        assets=assets,
    )
    return library.recompute_totals()


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "media"
    directory.mkdir()
    return directory
