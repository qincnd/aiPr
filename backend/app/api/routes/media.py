"""素材相关接口。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from app.api.deps import get_settings_dep
from app.core.config import Settings
from app.models.api import MediaScanResponse, ScanRequest
from app.services.media_scanner import find_ffprobe, get_library

router = APIRouter(prefix="/media", tags=["media"])


@router.get("/roots")
async def media_roots(settings: Settings = Depends(get_settings_dep)) -> dict[str, object]:
    """前端用来提示「哪些目录允许被扫描」。"""

    return {
        "roots": [str(path) for path in settings.media_root_paths],
        "ffprobe_available": bool(find_ffprobe(settings.ffprobe_path)),
    }


@router.post("/scan", response_model=MediaScanResponse)
async def scan(
    payload: ScanRequest,
    settings: Settings = Depends(get_settings_dep),
) -> MediaScanResponse:
    library = await asyncio.to_thread(
        get_library,
        payload.path,
        settings=settings,
        recursive=payload.recursive,
        probe=payload.probe,
        force=True,
    )
    return MediaScanResponse(
        library=library,
        ffprobe_available=bool(find_ffprobe(settings.ffprobe_path)),
    )
