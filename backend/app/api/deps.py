"""FastAPI 依赖：从 app.state 里取服务实例。"""

from __future__ import annotations

from fastapi import Request

from app.core.config import Settings
from app.services.jobs.manager import JobManager
from app.services.planner.service import PlannerService
from app.services.premiere.base import PremiereAdapter


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_adapter(request: Request) -> PremiereAdapter:
    return request.app.state.adapter


def get_planner(request: Request) -> PlannerService:
    return request.app.state.planner


def get_jobs(request: Request) -> JobManager:
    return request.app.state.jobs
