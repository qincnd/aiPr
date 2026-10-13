"""启发式方案生成器（不依赖大模型）。

用途有两个：
1. 没配 API Key / 大模型调用失败时的兜底，保证整条链路仍然可跑；
2. 作为大模型输出的对照基线（回归测试里很好用）。
"""

from __future__ import annotations

import re

from app.core.errors import PlanValidationError
from app.core.logging import get_logger
from app.models.api import PlanRequest, PlanResponse
from app.models.media import MediaLibrary
from app.models.plan import (
    AppendClipOp,
    ApplyTransitionOp,
    ClipRef,
    CreateSequenceOp,
    EditPlan,
    ExportSequenceOp,
    OperationType,
    SequenceSpec,
    SetVolumeOp,
)

from .base import BasePlanner, pick_audio, pick_videos

logger = get_logger(__name__)

DEFAULT_TARGET_DURATION = 30.0
MIN_CLIP_DURATION = 1.0
MAX_CLIP_DURATION = 8.0
DURATION_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:秒|秒钟|s\b|sec|seconds)", re.IGNORECASE)
MUSIC_KEYWORDS = ("音乐", "配乐", "bgm", "背景音", "背景音乐")
EXPORT_KEYWORDS = ("导出", "输出", "成片", "渲染", "export")


class HeuristicPlanner(BasePlanner):
    name = "heuristic"

    async def create_plan(self, request: PlanRequest, library: MediaLibrary) -> PlanResponse:
        options = request.options
        videos = pick_videos(library, request.media_ids)
        audios = pick_audio(library, request.media_ids)
        if not videos:
            raise PlanValidationError("素材库里没有可用的视频文件，无法生成剪辑方案")

        target_duration = (
            options.target_duration
            or self._parse_duration(request.prompt)
            or DEFAULT_TARGET_DURATION
        )
        clip_count = max(1, min(options.max_clips, len(videos)))
        chosen = self._select_clips(videos, clip_count)
        per_clip = max(MIN_CLIP_DURATION, target_duration / len(chosen))

        sequence = SequenceSpec(
            name="AI Sequence",
            width=options.width,
            height=options.height,
            fps=options.fps,
            preset=options.preset,
        )

        operations: list = [
            CreateSequenceOp(
                op=OperationType.CREATE_SEQUENCE,
                name=sequence.name,
                width=options.width,
                height=options.height,
                fps=options.fps,
                preset=options.preset,
                note=f"建立 {sequence.label} 序列",
            )
        ]

        durations: list[float] = []
        for index, asset in enumerate(chosen, start=1):
            source_duration = asset.duration_seconds or per_clip
            take = round(min(per_clip, source_duration), 2)
            take = max(take, MIN_CLIP_DURATION)
            in_point = round(max(0.0, (source_duration - take) / 2), 2)
            durations.append(take)
            operations.append(
                AppendClipOp(
                    op=OperationType.APPEND_CLIP,
                    media=asset.id,
                    ref=f"clip_{index}",
                    track="V1",
                    in_point=in_point,
                    out_point=round(in_point + take, 2),
                    note=f"按顺序排入第 {index} 个镜头：{asset.name}",
                )
            )

        cursor = 0.0
        for index in range(1, len(durations)):
            cursor = round(cursor + durations[index - 1], 2)
            transition_duration = round(min(1.0, min(durations[index - 1], durations[index]) / 4), 2)
            if transition_duration < 0.2:
                continue
            operations.append(
                ApplyTransitionOp(
                    op=OperationType.APPLY_TRANSITION,
                    at=cursor,
                    track="V1",
                    transition="cross_dissolve",
                    duration=transition_duration,
                    note="相邻镜头之间加叠化过渡",
                )
            )

        total_duration = round(sum(durations), 2)

        if audios and any(keyword in request.prompt.lower() for keyword in MUSIC_KEYWORDS):
            bgm = audios[0]
            bgm_length = round(min(total_duration, bgm.duration_seconds or total_duration), 2)
            operations.append(
                AppendClipOp(
                    op=OperationType.APPEND_CLIP,
                    media=bgm.id,
                    ref="bgm",
                    track="A1",
                    in_point=0.0,
                    out_point=bgm_length,
                    note=f"铺一条背景音乐：{bgm.name}",
                )
            )
            operations.append(
                SetVolumeOp(
                    op=OperationType.SET_VOLUME,
                    clip=ClipRef(ref="bgm"),
                    db=-12.0,
                    note="背景音乐压低到 -12dB，避免盖住人声",
                )
            )

        if any(keyword in request.prompt.lower() for keyword in EXPORT_KEYWORDS):
            operations.append(
                ExportSequenceOp(
                    op=OperationType.EXPORT_SEQUENCE,
                    output="ai_edit_output.mp4",
                    format="H.264",
                    note="导出 H.264 成片",
                )
            )

        plan = EditPlan(
            project_name="ai_edit_project",
            summary=(
                f"使用 {len(chosen)} 个镜头，目标成片约 {target_duration:g} 秒"
                f"（实际 {total_duration:g} 秒），"
                f"镜头之间以叠化过渡，序列规格 {sequence.label}。"
            ),
            sequence=sequence,
            operations=operations,
            source="heuristic",
        )
        warnings = [] if library.probed else ["未检测到 ffprobe，画面时长按默认值估算，建议安装 ffmpeg"]
        return PlanResponse(plan=plan, library=library, planner=self.name, warnings=warnings)

    @staticmethod
    def _parse_duration(prompt: str) -> float | None:
        match = DURATION_PATTERN.search(prompt)
        if not match:
            return None
        try:
            value = float(match.group(1))
        except ValueError:  # pragma: no cover
            return None
        return value if 1.0 <= value <= 3600.0 else None

    @staticmethod
    def _select_clips(videos: list, count: int) -> list:
        """素材多时等间隔抽样，保证节奏均匀且用上整段素材的不同部分。"""

        ordered = sorted(videos, key=lambda item: item.relative_path.lower())
        if len(ordered) <= count:
            return ordered
        step = len(ordered) / count
        return [ordered[min(int(index * step), len(ordered) - 1)] for index in range(count)]
