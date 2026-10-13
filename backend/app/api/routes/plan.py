"""方案生成接口。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from app.api.deps import get_planner, get_settings_dep
from app.core.config import Settings
from app.models.api import PlanRequest, PlanResponse
from app.services.media_scanner import get_library
from app.services.planner.service import PlannerService

router = APIRouter(tags=["plan"])


@router.post("/plan", response_model=PlanResponse)
async def create_plan(
    payload: PlanRequest,
    settings: Settings = Depends(get_settings_dep),
    planner: PlannerService = Depends(get_planner),
) -> PlanResponse:
    """只生成方案，不碰 Premiere。前端可以先把方案展示给用户确认。"""

    library = await asyncio.to_thread(
        get_library, payload.media_root, settings=settings, force=True
    )
    return await planner.create_plan(payload, library)
