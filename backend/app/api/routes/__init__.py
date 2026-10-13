"""HTTP / WebSocket 路由。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import health, jobs, media, plan, system

api_router = APIRouter(prefix="/api")
api_router.include_router(health.router)
api_router.include_router(system.router)
api_router.include_router(media.router)
api_router.include_router(plan.router)
api_router.include_router(jobs.router)

__all__ = ["api_router"]
