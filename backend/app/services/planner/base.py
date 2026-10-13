"""方案生成器的公共接口与校验工具。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator

from app.models.api import PlanRequest, PlanResponse
from app.models.media import MediaKind, MediaLibrary
from app.models.plan import AppendClipOp, EditPlan, ImportMediaOp, InsertClipOp


def select_assets(library: MediaLibrary, media_ids: list[str]) -> list:
    """按用户勾选的素材过滤素材库；未勾选则返回全部。"""

    if not media_ids:
        return list(library.assets)
    index = library.build_index()
    selected, seen = [], set()
    for key in media_ids:
        asset = index.get(key.strip().lower())
        if asset and asset.id not in seen:
            seen.add(asset.id)
            selected.append(asset)
    return selected


def iter_referenced_media(plan: EditPlan) -> Iterator[tuple[int, str]]:
    for position, op in enumerate(plan.operations):
        if isinstance(op, ImportMediaOp):
            for key in op.media:
                yield position, key
        elif isinstance(op, (AppendClipOp, InsertClipOp)):
            yield position, op.media


def validate_media_references(plan: EditPlan, library: MediaLibrary) -> list[str]:
    """检查方案引用的素材是否都存在，返回警告列表（不抛异常）。"""

    index = library.build_index()
    warnings: list[str] = []
    for position, key in iter_referenced_media(plan):
        if index.get(key.strip().lower()):
            continue
        if Path(key).is_absolute():
            continue
        warnings.append(f"第 {position + 1} 条操作引用了素材库里不存在的素材「{key}」")
    return warnings


def pick_videos(library: MediaLibrary, media_ids: list[str]) -> list:
    return [item for item in select_assets(library, media_ids) if item.kind is MediaKind.VIDEO]


def pick_audio(library: MediaLibrary, media_ids: list[str]) -> list:
    return [item for item in select_assets(library, media_ids) if item.kind is MediaKind.AUDIO]


class BasePlanner(ABC):
    """把「提示词 + 素材库」变成「剪辑方案」。"""

    name: str = "base"

    @abstractmethod
    async def create_plan(self, request: PlanRequest, library: MediaLibrary) -> PlanResponse:
        raise NotImplementedError
