"""基于大模型的方案生成器。"""

from __future__ import annotations

from pydantic import ValidationError

from app.core.errors import LLMError, PlanValidationError
from app.core.logging import get_logger
from app.models.api import PlanOptions, PlanRequest, PlanResponse
from app.models.media import MediaLibrary
from app.models.plan import EditPlan
from app.services.llm.client import LLMClient

from .base import BasePlanner, validate_media_references
from .prompts import build_retry_prompt, build_system_prompt, build_user_prompt

logger = get_logger(__name__)

MAX_ATTEMPTS = 2


class LLMPlanner(BasePlanner):
    name = "llm"

    def __init__(self, client: LLMClient) -> None:
        self.client = client

    async def create_plan(self, request: PlanRequest, library: MediaLibrary) -> PlanResponse:
        options: PlanOptions = request.options
        system = build_system_prompt()
        base_user_prompt = build_user_prompt(
            prompt=request.prompt,
            library=library,
            media_ids=request.media_ids,
            width=options.width,
            height=options.height,
            fps=options.fps,
            target_duration=options.target_duration,
            max_clips=options.max_clips,
            extra_instructions=options.extra_instructions,
            preset=options.preset,
        )

        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            user = base_user_prompt
            if last_error is not None:
                user = f"{base_user_prompt}\n\n{build_retry_prompt(str(last_error))}"

            logger.info("请求大模型生成剪辑方案（第 %d/%d 次）", attempt, MAX_ATTEMPTS)
            data = await self.client.complete_json(system=system, user=user)

            try:
                plan = EditPlan.model_validate(data)
            except ValidationError as exc:
                last_error = PlanValidationError(_format_validation_error(exc))
                logger.warning("方案校验失败：%s", last_error)
                continue

            warnings = list(plan.warnings) + validate_media_references(plan, library)
            if warnings:
                logger.warning("方案存在问题：%s", warnings)
            return PlanResponse(
                plan=plan,
                library=library,
                planner=self.name,
                warnings=warnings,
            )

        raise last_error or LLMError("大模型未能生成合法方案")


def _format_validation_error(exc: ValidationError) -> str:
    """把 pydantic 的错误压缩成模型容易看懂的一段文本。"""

    lines = []
    for error in exc.errors()[:12]:
        location = ".".join(str(part) for part in error.get("loc", ()))
        lines.append(f"- {location or '<root>'}: {error.get('msg')}")
    return "\n".join(lines)
