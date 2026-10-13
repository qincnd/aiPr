"""方案执行器：把 Edit Plan 翻译成一连串对 Premiere 适配器的调用。

这是唯一一个「懂方案语义」的地方：适配器只负责单条操作怎么落地，
而「片段引用怎么解析、进度怎么上报、失败要不要继续」都由这里统一处理。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from app.core.config import Settings, assert_allowed_output_path
from app.core.errors import AiPrError, PlanValidationError, PremiereError
from app.core.logging import get_logger
from app.models.job import EventLevel
from app.models.media import MediaAsset, MediaKind, MediaLibrary
from app.models.plan import (
    AddMarkerOp,
    AppendClipOp,
    ApplyEffectOp,
    ApplyTransitionOp,
    ClipRef,
    CreateSequenceOp,
    DeleteRangeOp,
    EditPlan,
    ExportSequenceOp,
    ImportMediaOp,
    InsertClipOp,
    MoveClipOp,
    OperationType,
    RazorOp,
    SequenceSpec,
    SetVolumeOp,
    TrimClipOp,
)
from app.models.timeline import ClipHandle, TimelineState
from app.services.media_scanner import describe_media_file
from app.services.premiere.base import PremiereAdapter

logger = get_logger(__name__)

EventEmitter = Callable[..., Awaitable[None]]


class ExecutionResult(BaseModel):
    success: bool = True
    operations_total: int = 0
    operations_done: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    timeline: TimelineState | None = None


class PlanExecutor:
    def __init__(
        self,
        adapter: PremiereAdapter,
        settings: Settings,
        emit: EventEmitter,
        *,
        run_dir: Path | None = None,
    ) -> None:
        self.adapter = adapter
        self.settings = settings
        self.emit = emit
        self.run_dir = Path(run_dir) if run_dir else None
        self.library: MediaLibrary | None = None
        self.plan: EditPlan | None = None
        self._handles: dict[str, ClipHandle] = {}
        self._artifacts: list[str] = []

    async def run(
        self,
        plan: EditPlan,
        *,
        library: MediaLibrary,
        dry_run: bool = False,
        continue_on_error: bool = True,
    ) -> ExecutionResult:
        self.plan = plan
        self.library = library
        operations = plan.enabled_operations()
        result = ExecutionResult(operations_total=len(operations))
        result.skipped = len(plan.operations) - len(operations)

        await self.emit(
            EventLevel.INFO,
            f"开始执行方案「{plan.project_name}」，共 {len(operations)} 条操作"
            + ("（预演模式：不写工程、不导出）" if dry_run else ""),
        )
        for warning in plan.warnings:
            await self.emit(EventLevel.WARNING, f"方案提醒：{warning}")

        await self._open_project(dry_run=dry_run)

        for index, operation in enumerate(operations):
            label = operation.op.value
            await self.emit(
                EventLevel.INFO,
                f"[{index + 1}/{len(operations)}] {OPERATION_LABELS.get(operation.op, label)}"
                + (f" —— {operation.note}" if operation.note else ""),
                op_index=index,
                op=label,
            )
            try:
                await self._dispatch(operation, index, dry_run=dry_run)
                result.operations_done += 1
            except AiPrError as exc:
                result.errors.append(f"{label}: {exc}")
                await self.emit(
                    EventLevel.ERROR,
                    f"操作失败（{label}）：{exc}",
                    op_index=index,
                    op=label,
                    data={"code": getattr(exc, "code", "error")},
                )
                if isinstance(operation, CreateSequenceOp):
                    # 建序列失败是致命错误：继续执行会画到 Premiere 里当前打开的无关序列上
                    result.success = False
                    await self.emit(
                        EventLevel.ERROR,
                        "新建序列失败，后续操作不再执行（避免改到 Premiere 里当前打开的其它序列）",
                    )
                    break
                if not continue_on_error:
                    result.success = False
                    break
            except Exception as exc:  # noqa: BLE001 - 未知异常也要记录，避免整个任务直接崩掉
                logger.exception("执行 %s 时发生未知异常", label)
                result.errors.append(f"{label}: {exc}")
                await self.emit(
                    EventLevel.ERROR,
                    f"操作出现未预期错误（{label}）：{exc}",
                    op_index=index,
                    op=label,
                )
                if not continue_on_error or isinstance(operation, CreateSequenceOp):
                    result.success = False
                    break

        if result.errors:
            result.success = False

        if not dry_run:
            await self._save_project()

        result.timeline = await self._snapshot()
        if result.timeline is not None:
            await self.emit(
                EventLevel.INFO,
                f"时间线快照：{len(result.timeline.tracks)} 条轨道，"
                f"总时长 {result.timeline.duration:.2f}s",
                data={"timeline": result.timeline.model_dump()},
            )

        result.artifacts = list(self._artifacts)
        await self.emit(
            EventLevel.SUCCESS if result.success else EventLevel.WARNING,
            f"执行结束：成功 {result.operations_done}/{result.operations_total} 条操作"
            + (f"，失败 {len(result.errors)} 条" if result.errors else ""),
        )
        return result

    # ---------------------------------------------------------------- 生命周期
    async def _open_project(self, *, dry_run: bool) -> None:
        project_path = None if dry_run else self.settings.premiere_project_path
        await self._call(self.adapter.open_project, project_path)

    async def _save_project(self) -> None:
        try:
            saved = await self._call(self.adapter.save)
        except Exception as exc:  # noqa: BLE001 - 保存失败不该让整个任务算失败，只提示
            await self.emit(EventLevel.WARNING, f"保存工程失败：{exc}")
            return
        if saved:
            self._artifacts.append(str(saved))
            await self.emit(EventLevel.INFO, f"工程已保存：{saved}")

    async def _snapshot(self) -> TimelineState | None:
        try:
            return await self._call(self.adapter.snapshot)
        except AiPrError:
            return None

    # ---------------------------------------------------------------- 分发
    async def _dispatch(self, operation: Any, index: int, *, dry_run: bool) -> None:
        if dry_run and isinstance(operation, ExportSequenceOp):
            await self.emit(EventLevel.DEBUG, "预演模式：跳过导出")
            return

        match operation:
            case ImportMediaOp():
                await self._op_import_media(operation)
            case CreateSequenceOp():
                await self._op_create_sequence(operation, index)
            case AppendClipOp():
                await self._op_append_clip(operation, index)
            case InsertClipOp():
                await self._op_insert_clip(operation, index)
            case MoveClipOp():
                await self._op_move_clip(operation)
            case TrimClipOp():
                await self._op_trim_clip(operation)
            case RazorOp():
                await self._call(self.adapter.razor, at=operation.at, track=operation.track)
            case DeleteRangeOp():
                await self._call(
                    self.adapter.delete_range,
                    start=operation.start,
                    end=operation.end,
                    track=operation.track,
                    ripple=operation.ripple,
                )
            case ApplyTransitionOp():
                await self._call(
                    self.adapter.apply_transition,
                    at=operation.at,
                    track=operation.track,
                    transition=operation.transition,
                    duration=operation.duration,
                )
            case ApplyEffectOp():
                handle = await self._resolve_clip(operation.clip)
                await self._call(
                    self.adapter.apply_effect,
                    handle,
                    effect=operation.effect,
                    params=operation.params,
                )
            case SetVolumeOp():
                handle = await self._resolve_clip(operation.clip)
                await self._call(self.adapter.set_volume, handle, db=operation.db)
            case AddMarkerOp():
                await self._call(
                    self.adapter.add_marker,
                    at=operation.at,
                    name=operation.name,
                    color=operation.color,
                    duration=operation.duration,
                )
            case ExportSequenceOp():
                await self._op_export(operation, index)
            case _:  # pragma: no cover - 新增枚举值但忘了实现时走到这里
                raise PlanValidationError(f"执行器不支持的操作：{operation.op}")

    # ---------------------------------------------------------------- 单条操作
    async def _op_import_media(self, operation: ImportMediaOp) -> None:
        keys = operation.media or (self.plan.referenced_media() if self.plan else [])
        assets = [self._resolve_media(key) for key in keys]
        if not assets:
            raise PlanValidationError("import_media 没有可导入的素材")
        names = await self._call(self.adapter.import_media, assets, operation.bin)
        preview = "、".join(names[:5]) + ("…" if len(names) > 5 else "")
        await self.emit(EventLevel.INFO, f"导入 {len(names)} 个素材：{preview}")

    async def _op_create_sequence(self, operation: CreateSequenceOp, index: int) -> None:
        spec = SequenceSpec(
            name=operation.name,
            width=operation.width,
            height=operation.height,
            fps=operation.fps,
            preset=operation.preset,
        )
        timeline = await self._call(
            self.adapter.create_sequence, spec, self._reference_assets()
        )
        await self.emit(
            EventLevel.INFO,
            f"序列「{spec.name}」已就绪（{spec.label}）",
            op_index=index,
            op=operation.op.value,
            data={"timeline": timeline.model_dump() if timeline else None},
        )

    def _reference_assets(self) -> list[MediaAsset]:
        """方案里会用到的视频素材（最多取前几个），给真实后端当建序列的规格参照。"""

        assets: list[MediaAsset] = []
        for key in (self.plan.referenced_media() if self.plan else []):
            try:
                asset = self._resolve_media(key)
            except AiPrError:
                continue
            if asset.kind is MediaKind.VIDEO:
                assets.append(asset)
        return assets[:3]

    async def _op_append_clip(self, operation: AppendClipOp, index: int) -> None:
        asset = self._resolve_media(operation.media)
        handle = await self._call(
            self.adapter.append_clip,
            asset,
            ref=operation.ref or "",
            track=operation.track,
            in_point=operation.in_point,
            out_point=operation.out_point,
        )
        self._remember(operation.ref, handle)
        await self.emit(
            EventLevel.INFO,
            f"追加 {asset.name} → {handle.track} [{handle.start:.2f}s → {handle.end:.2f}s]",
            op_index=index,
            op=operation.op.value,
            data={"clip": handle.model_dump()},
        )

    async def _op_insert_clip(self, operation: InsertClipOp, index: int) -> None:
        asset = self._resolve_media(operation.media)
        handle = await self._call(
            self.adapter.insert_clip,
            asset,
            ref=operation.ref or "",
            track=operation.track,
            at=operation.at,
            in_point=operation.in_point,
            out_point=operation.out_point,
        )
        self._remember(operation.ref, handle)
        await self.emit(
            EventLevel.INFO,
            f"插入 {asset.name} → {handle.track} @{operation.at:.2f}s",
            op_index=index,
            op=operation.op.value,
            data={"clip": handle.model_dump()},
        )

    async def _op_move_clip(self, operation: MoveClipOp) -> None:
        handle = await self._resolve_clip(operation.clip)
        moved = await self._call(
            self.adapter.move_clip, handle, to_time=operation.to_time, track=operation.track
        )
        self._remember_ref(handle.ref, moved)

    async def _op_trim_clip(self, operation: TrimClipOp) -> None:
        handle = await self._resolve_clip(operation.clip)
        trimmed = await self._call(
            self.adapter.trim_clip,
            handle,
            in_point=operation.in_point,
            out_point=operation.out_point,
        )
        self._remember_ref(handle.ref, trimmed)

    async def _op_export(self, operation: ExportSequenceOp, index: int) -> None:
        output = assert_allowed_output_path(operation.output, self.settings.exports_dir)
        artifacts = await self._call(
            self.adapter.export_sequence,
            output=output,
            preset=operation.preset,
            format=operation.format,
        )
        paths = [str(item) for item in artifacts]
        self._artifacts.extend(paths)
        await self.emit(
            EventLevel.INFO,
            f"导出完成，产出 {len(paths)} 个文件",
            op_index=index,
            op=operation.op.value,
            data={"artifacts": paths},
        )

    # ---------------------------------------------------------------- 解析
    def _resolve_media(self, key: str) -> MediaAsset:
        if self.library is not None:
            asset = self.library.find(key)
            if asset:
                return asset

        path = Path(key)
        if path.is_absolute():
            return describe_media_file(path, settings=self.settings)

        available = ", ".join(item.name for item in (self.library.assets[:8] if self.library else []))
        raise PlanValidationError(
            f"素材库里找不到「{key}」。请确认它位于素材目录内"
            + (f"（可用素材示例：{available}）" if available else "")
        )

    async def _resolve_clip(self, clip_ref: ClipRef) -> ClipHandle:
        if clip_ref.ref:
            handle = self._handles.get(clip_ref.ref)
            if handle:
                return handle
            for candidate in self._handles.values():
                if candidate.ref == clip_ref.ref:
                    return candidate
            raise PlanValidationError(f"找不到名为「{clip_ref.ref}」的片段，请先用 ref 命名它")

        if clip_ref.track is None:
            raise PlanValidationError(f"片段引用信息不足：{clip_ref.describe()}")

        timeline = await self._snapshot()
        if timeline is None:
            raise PremiereError("当前后端拿不到时间线快照，无法按轨道定位片段，请改用 ref 引用")

        track = timeline.track(clip_ref.track)
        if track is None:
            raise PlanValidationError(f"轨道不存在：{clip_ref.track}")
        clips = sorted(track.clips, key=lambda item: item.start)

        if clip_ref.index is not None:
            if clip_ref.index >= len(clips):
                raise PlanValidationError(
                    f"轨道 {clip_ref.track} 上只有 {len(clips)} 个片段，取不到 index={clip_ref.index}"
                )
            chosen = clips[clip_ref.index]
        elif clip_ref.at is not None:
            matches = [
                clip for clip in clips if clip.start - 1e-6 <= clip_ref.at <= clip.end + 1e-6
            ]
            if not matches:
                raise PlanValidationError(f"轨道 {clip_ref.track} 在 {clip_ref.at}s 处没有片段")
            chosen = matches[0]
        else:
            raise PlanValidationError(f"片段引用信息不足：{clip_ref.describe()}")

        return ClipHandle(
            ref=chosen.id,
            track=chosen.track,
            media_ref=chosen.media_ref,
            start=chosen.start,
            end=chosen.end,
            source_in=chosen.source_in,
            source_out=chosen.source_out,
        )

    def _remember(self, ref: str | None, handle: ClipHandle) -> None:
        if ref:
            self._handles[ref] = handle
        self._handles.setdefault(handle.ref, handle)

    def _remember_ref(self, old_ref: str, handle: ClipHandle) -> None:
        self._handles[old_ref] = handle
        self._handles[handle.ref] = handle

    # ---------------------------------------------------------------- 工具
    async def _call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """适配器调用多为阻塞 IO（pymiere 走同步通道），统一下沉到线程执行。"""

        return await asyncio.to_thread(func, *args, **kwargs)


OPERATION_LABELS: dict[OperationType, str] = {
    OperationType.IMPORT_MEDIA: "导入素材",
    OperationType.CREATE_SEQUENCE: "新建序列",
    OperationType.APPEND_CLIP: "追加片段",
    OperationType.INSERT_CLIP: "插入片段",
    OperationType.MOVE_CLIP: "移动片段",
    OperationType.TRIM_CLIP: "修剪片段",
    OperationType.RAZOR: "切割",
    OperationType.DELETE_RANGE: "删除区间",
    OperationType.APPLY_TRANSITION: "添加转场",
    OperationType.APPLY_EFFECT: "添加效果",
    OperationType.SET_VOLUME: "调整音量",
    OperationType.ADD_MARKER: "添加标记",
    OperationType.EXPORT_SEQUENCE: "导出序列",
}
