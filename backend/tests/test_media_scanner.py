"""素材扫描测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import MediaAccessError
from app.models.media import MediaKind
from app.services.media_scanner import (
    clear_library_cache,
    describe_media_file,
    get_library,
    scan_media,
)


def build_settings(media_root: Path, **overrides) -> Settings:
    values = {
        "media_roots": str(media_root),
        "workspace_dir": media_root.parent / "workspace",
        "llm_api_key": None,
    }
    values.update(overrides)
    return Settings(**values)


def test_scan_classifies_and_sorts(media_dir: Path):
    (media_dir / "b_clip.MP4").write_bytes(b"x")
    (media_dir / "a_clip.mov").write_bytes(b"x")
    (media_dir / "bgm.wav").write_bytes(b"x")
    (media_dir / "cover.png").write_bytes(b"x")
    (media_dir / "notes.txt").write_bytes(b"x")
    (media_dir / "sub").mkdir()
    (media_dir / "sub" / "nested.mp4").write_bytes(b"x")

    library = scan_media(media_dir, settings=build_settings(media_dir), probe=False)

    names = [asset.name for asset in library.assets]
    assert "notes.txt" not in names  # 非媒体文件被忽略
    assert names == ["a_clip.mov", "b_clip.MP4", "bgm.wav", "cover.png", "nested.mp4"]
    assert library.totals["video"] == 3
    assert library.totals["audio"] == 1
    assert library.totals["image"] == 1
    assert library.assets[0].extension == ".mov"


def test_scan_can_skip_subdirectories(media_dir: Path):
    (media_dir / "top.mp4").write_bytes(b"x")
    (media_dir / "sub").mkdir()
    (media_dir / "sub" / "nested.mp4").write_bytes(b"x")

    library = scan_media(media_dir, settings=build_settings(media_dir), recursive=False, probe=False)

    assert [asset.name for asset in library.assets] == ["top.mp4"]
    assert library.recursive is False


def test_scan_respects_max_files(media_dir: Path):
    for index in range(5):
        (media_dir / f"clip_{index}.mp4").write_bytes(b"x")

    library = scan_media(media_dir, settings=build_settings(media_dir, max_media_files=3), probe=False)

    assert len(library.assets) == 3
    assert library.skipped and "max_media_files" in library.skipped[0]


def test_path_outside_media_roots_is_rejected(tmp_path: Path, media_dir: Path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.mp4").write_bytes(b"x")

    with pytest.raises(MediaAccessError):
        scan_media(outside, settings=build_settings(media_dir), probe=False)


def test_missing_path_is_rejected(media_dir: Path):
    with pytest.raises(MediaAccessError):
        scan_media(media_dir / "not-there", settings=build_settings(media_dir), probe=False)


def test_library_lookup_by_id_name_and_stem(media_dir: Path):
    (media_dir / "intro_shot.mp4").write_bytes(b"x")
    library = scan_media(media_dir, settings=build_settings(media_dir), probe=False)
    asset = library.assets[0]

    assert library.find(asset.id) is not None
    assert library.find("intro_shot.mp4") is not None
    assert library.find("INTRO_SHOT") is not None
    assert library.find("不存在.mp4") is None


def test_get_library_uses_cache(media_dir: Path):
    (media_dir / "a.mp4").write_bytes(b"x")
    clear_library_cache()
    settings = build_settings(media_dir)

    first = get_library(media_dir, settings=settings, probe=False)
    (media_dir / "b.mp4").write_bytes(b"x")
    cached = get_library(media_dir, settings=settings, probe=False)
    assert [asset.name for asset in cached.assets] == [asset.name for asset in first.assets]

    refreshed = get_library(media_dir, settings=settings, probe=False, force=True)
    assert len(refreshed.assets) == 2


def test_describe_media_file_handles_single_file(media_dir: Path):
    target = media_dir / "solo.mp4"
    target.write_bytes(b"1234")

    asset = describe_media_file(target, settings=build_settings(media_dir), probe=False)

    assert asset.kind is MediaKind.VIDEO
    assert asset.size_bytes == 4
    assert asset.probe_source == "metadata-only"
