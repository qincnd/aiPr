"""FastAPI 应用入口。

启动：
    cd backend
    ..\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.routes import api_router
from app.core.config import PROJECT_ROOT, Settings, get_settings
from app.core.errors import AiPrError
from app.core.logging import configure_logging, get_logger
from app.services.jobs.manager import JobManager
from app.services.planner.service import PlannerService
from app.services.premiere.factory import create_adapter

logger = get_logger(__name__)

ERROR_STATUS: dict[str, int] = {
    "media_access_denied": 403,
    "plan_invalid": 422,
    "llm_error": 502,
    "premiere_unavailable": 503,
    "premiere_error": 500,
}

FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = get_settings()
    configure_logging(settings.debug)
    settings.prepare_dirs()

    adapter = create_adapter(settings)
    planner = PlannerService(settings)
    app.state.settings = settings
    app.state.adapter = adapter
    app.state.planner = planner
    app.state.jobs = JobManager(settings, planner, adapter)

    logger.info("aiPr %s 启动：Premiere 后端=%s，大模型=%s", __version__, adapter.name, planner.llm_ready)
    try:
        yield
    finally:
        await app.state.jobs.shutdown()
        adapter.close()
        logger.info("aiPr 已停止")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="aiPr —— 大模型驱动的 Premiere Pro 剪辑服务",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router)

    @app.exception_handler(AiPrError)
    async def _handle_aipr_error(_: Request, exc: AiPrError) -> JSONResponse:
        return JSONResponse(
            status_code=ERROR_STATUS.get(exc.code, 500),
            content={"code": exc.code, "message": exc.message},
        )

    if FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
        logger.info("已挂载前端构建产物：%s", FRONTEND_DIST)
    else:
        @app.get("/")
        async def index() -> dict[str, str]:
            return {
                "message": "前端尚未构建。开发时请运行 `npm run dev`（frontend 目录），"
                "或执行 `npm run build` 让后端直接托管。",
                "docs": "/docs",
            }

    return app


app = create_app()
