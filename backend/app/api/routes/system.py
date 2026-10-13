"""系统信息：后端能力、素材根目录、大模型配置状态。"""

from __future__ import annotations

import platform
import sys

from fastapi import APIRouter, Depends

from app import __version__
from app.api.deps import get_adapter, get_planner, get_settings_dep
from app.core.config import Settings
from app.models.api import SystemInfo
from app.models.media import MediaLibrary
from app.services.media_scanner import find_ffprobe
from app.services.planner.service import PlannerService
from app.services.premiere.base import PremiereAdapter

router = APIRouter(tags=["system"])


@router.get("/system", response_model=SystemInfo)
async def system_info(
    settings: Settings = Depends(get_settings_dep),
    adapter: PremiereAdapter = Depends(get_adapter),
    planner: PlannerService = Depends(get_planner),
) -> SystemInfo:
    status = adapter.status()
    ffprobe = find_ffprobe(settings.ffprobe_path)
    return SystemInfo(
        app_name=settings.app_name,
        version=__version__,
        python_version=f"{platform.python_version()} ({sys.platform})",
        premiere=status.to_backend_info(),
        llm_enabled=settings.llm_enabled,
        llm_configured=planner.llm_ready,
        llm_model=settings.llm_model,
        llm_base_url=settings.llm_base_url,
        ffprobe_available=bool(ffprobe),
        ffprobe_path=ffprobe,
        media_roots=[str(path) for path in settings.media_root_paths],
        workspace_dir=str(settings.workspace_dir),
    )
