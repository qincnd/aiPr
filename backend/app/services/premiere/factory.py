"""适配器工厂：根据配置挑一个 Premiere 后端。

要接入新后端（自研 CEP 面板 / UXP 插件 / 生成 prproj 等），
实现 :class:`~app.services.premiere.base.PremiereAdapter` 后在这里注册即可。
"""

from __future__ import annotations

from typing import Callable

from app.core.config import Settings
from app.core.logging import get_logger

from .base import PremiereAdapter
from .mock import MockPremiereAdapter
from .pymiere_adapter import PymierePremiereAdapter

logger = get_logger(__name__)

BACKENDS: dict[str, Callable[[Settings], PremiereAdapter]] = {
    "mock": lambda settings: MockPremiereAdapter(settings.workspace_dir / "mock"),
    "pymiere": PymierePremiereAdapter,
}


def create_adapter(settings: Settings) -> PremiereAdapter:
    factory = BACKENDS.get(settings.premiere_backend)
    if factory is None:
        logger.warning(
            "未知的 premiere_backend=%s，回退到 mock（可选：%s）",
            settings.premiere_backend,
            ", ".join(sorted(BACKENDS)),
        )
        factory = BACKENDS["mock"]
    adapter = factory(settings)
    logger.info("Premiere 后端：%s（%s）", adapter.name, adapter.description)
    return adapter


def available_backends() -> list[str]:
    return sorted(BACKENDS)
