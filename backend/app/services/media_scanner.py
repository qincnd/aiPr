"""素材扫描服务。

扫描用户在前端填写的素材目录，产出 :class:`MediaLibrary`。
优先调用 ffprobe 读取真实时长/分辨率；未安装 ffprobe 时自动降级为「只读文件元数据」，
框架依然可以跑通（Mock 后端不依赖精确时长）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import Settings, assert_allowed_media_path
from app.core.errors import MediaAccessError
from app.core.logging import get_logger
from app.models.media import (
    ALL_EXTENSIONS,
    MediaAsset,
    MediaKind,
    MediaLibrary,
    classify_extension,
)

logger = get_logger(__name__)

PROBE_TIMEOUT_SECONDS = 20
CACHE_TTL_SECONDS = 30
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_library_cache: dict[tuple[str, bool, bool], tuple[float, MediaLibrary]] = {}


def find_ffprobe(configured: str | None = None) -> str | None:
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate)
        logger.warning("配置的 ffprobe 路径不存在：%s", configured)
    return shutil.which("ffprobe")


def _to_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _parse_fraction(value: Any) -> float | None:
    """解析 ffprobe 的 `30000/1001` 形式帧率。"""

    if not value or not isinstance(value, str):
        return None
    if "/" in value:
        numerator, _, denominator = value.partition("/")
        top, bottom = _to_float(numerator), _to_float(denominator)
        if not top or not bottom:
            return None
        return round(top / bottom, 3)
    return _to_float(value)


def _probe_with_ffprobe(ffprobe: str, path: Path) -> dict[str, Any] | None:
    command = [
        ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("ffprobe 调用失败 %s：%s", path, exc)
        return None
    if completed.returncode != 0:
        return None
    try:
        return json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


def _extract_probe_fields(probe: dict[str, Any], kind: MediaKind) -> dict[str, Any]:
    streams = probe.get("streams") or []
    container = probe.get("format") or {}
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)

    duration = _to_float(container.get("duration"))
    if duration is None:
        duration = _to_float((video or audio or {}).get("duration"))

    fields: dict[str, Any] = {
        "duration_seconds": round(duration, 3) if duration is not None else None,
        "probe_source": "ffprobe",
    }
    if video and kind is not MediaKind.AUDIO:
        fields["width"] = video.get("width")
        fields["height"] = video.get("height")
        fields["fps"] = _parse_fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        fields["codec"] = video.get("codec_name")
    if audio:
        fields["sample_rate"] = int(audio["sample_rate"]) if audio.get("sample_rate") else None
        fields["channels"] = audio.get("channels")
        if fields.get("codec") is None:
            fields["codec"] = audio.get("codec_name")
    return fields


def _build_asset(
    path: Path,
    root: Path,
    probe: dict[str, Any] | None,
) -> MediaAsset:
    try:
        stat = path.stat()
        size_bytes = stat.st_size
        modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    except OSError:  # pragma: no cover - 文件可能在扫描途中被删除
        size_bytes = 0
        modified_at = None

    kind = classify_extension(path.suffix)
    probe_fields: dict[str, Any] = {}
    if probe is not None:
        try:
            probe_fields = _extract_probe_fields(probe, kind)
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("解析 ffprobe 结果失败 %s：%s", path, exc)

    try:
        relative = str(path.relative_to(root))
    except ValueError:  # pragma: no cover
        relative = path.name

    return MediaAsset(
        id=MediaAsset.make_id(path),
        path=str(path),
        name=path.name,
        stem=path.stem,
        relative_path=relative,
        kind=kind,
        extension=path.suffix.lower(),
        size_bytes=size_bytes,
        modified_at=modified_at,
        **probe_fields,
    )


def _iter_candidate_files(
    root: Path,
    recursive: bool,
    max_files: int,
) -> tuple[list[Path], list[str]]:
    skipped: list[str] = []
    files: list[Path] = []
    iterator = root.rglob("*") if recursive else root.iterdir()
    for entry in iterator:
        try:
            if not entry.is_file() or entry.name.startswith("."):
                continue
        except OSError:
            continue
        if entry.suffix.lower() not in ALL_EXTENSIONS:
            continue
        files.append(entry)
        if len(files) >= max_files:
            skipped.append(f"已达到 max_media_files={max_files} 上限，其余文件被忽略")
            break
    files.sort(key=lambda item: str(item).lower())
    return files, skipped


def scan_media(
    root_raw: str | Path,
    *,
    settings: Settings,
    recursive: bool = True,
    probe: bool = True,
) -> MediaLibrary:
    """扫描素材目录，返回 :class:`MediaLibrary`。"""

    root = assert_allowed_media_path(root_raw, settings.media_root_paths)
    if not root.is_dir():
        raise MediaAccessError(f"素材路径不是目录：{root}")

    ffprobe = find_ffprobe(settings.ffprobe_path) if probe else None
    files, skipped = _iter_candidate_files(root, recursive, settings.max_media_files)
    logger.info("扫描 %s：命中 %d 个候选文件（ffprobe=%s）", root, len(files), bool(ffprobe))

    probes: list[dict[str, Any] | None] = [None] * len(files)
    if ffprobe and files:
        workers = min(8, len(files))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            probes = list(pool.map(lambda item: _probe_with_ffprobe(ffprobe, item), files))

    assets = [
        _build_asset(path, root, probe_data)
        for path, probe_data in zip(files, probes)
    ]

    library = MediaLibrary(
        root=str(root),
        scanned_at=datetime.now(timezone.utc),
        recursive=recursive,
        probed=bool(ffprobe),
        assets=assets,
        skipped=skipped,
    )
    return library.recompute_totals()


def describe_media_file(
    path: Path,
    *,
    root: Path | None = None,
    settings: Settings | None = None,
    probe: bool = True,
) -> MediaAsset:
    """对单个文件做一次轻量探测，用于方案里直接写绝对路径的场景。"""

    resolved = Path(path).expanduser().resolve()
    base = Path(root).resolve() if root else resolved.parent
    ffprobe = find_ffprobe(settings.ffprobe_path if settings else None) if probe else None
    probe_data = _probe_with_ffprobe(ffprobe, resolved) if ffprobe else None
    return _build_asset(resolved, base, probe_data)


def get_library(
    root_raw: str | Path,
    *,
    settings: Settings,
    recursive: bool = True,
    probe: bool = True,
    force: bool = False,
) -> MediaLibrary:
    """带短 TTL 缓存的扫描入口，避免一次任务里反复遍历磁盘。"""

    key = (str(Path(root_raw).expanduser().resolve()).lower(), recursive, probe)
    now = time.monotonic()
    cached = _library_cache.get(key)
    if cached and not force and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    library = scan_media(root_raw, settings=settings, recursive=recursive, probe=probe)
    _library_cache[key] = (now, library)
    return library


def clear_library_cache() -> None:
    _library_cache.clear()
