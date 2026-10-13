"""方案生成服务：优先用大模型，失败时自动降级到启发式。"""

from __future__ import annotations

from app.core.config import Settings
from app.core.errors import AiPrError
from app.core.logging import get_logger
from app.models.api import PlanRequest, PlanResponse
from app.models.media import MediaLibrary
from app.services.llm.client import LLMClient

from .base import BasePlanner
from .heuristic_planner import HeuristicPlanner
from .llm_planner import LLMPlanner

logger = get_logger(__name__)


class PlannerService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = LLMClient(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            temperature=settings.llm_temperature,
        )
        self.heuristic = HeuristicPlanner()

    @property
    def llm_ready(self) -> bool:
        return self.settings.llm_available and self.client.configured

    async def create_plan(
        self,
        request: PlanRequest,
        library: MediaLibrary,
        *,
        allow_fallback: bool = True,
    ) -> PlanResponse:
        if request.options.use_llm and self.llm_ready:
            planner: BasePlanner = LLMPlanner(self.client)
            try:
                return await planner.create_plan(request, library)
            except Exception as exc:  # noqa: BLE001 - 大模型/网络的任何失败都必须能兜底
                if not allow_fallback:
                    raise
                logger.warning("大模型生成方案失败，回退到启发式方案：%s", exc)
                response = await self.heuristic.create_plan(request, library)
                response.warnings.insert(
                    0, f"大模型方案不可用（{type(exc).__name__}: {exc}），已回退到内置启发式排片"
                )
                return response

        if request.options.use_llm and not self.llm_ready:
            response = await self.heuristic.create_plan(request, library)
            response.warnings.insert(0, "未配置 AIPR_LLM_API_KEY，已使用内置启发式排片")
            return response

        return await self.heuristic.create_plan(request, library)
