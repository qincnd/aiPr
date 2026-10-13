"""剪辑方案（Edit Plan）DSL。

这是整个框架的「中间语言」：大模型只负责生成这份 JSON，
执行器再把它翻译成对 Premiere Pro 的具体调用。
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

TRACK_PATTERN = r"^[VA]\d+$"


class OperationType(str, Enum):
    IMPORT_MEDIA = "import_media"
    CREATE_SEQUENCE = "create_sequence"
    APPEND_CLIP = "append_clip"
    INSERT_CLIP = "insert_clip"
    MOVE_CLIP = "move_clip"
    TRIM_CLIP = "trim_clip"
    RAZOR = "razor"
    DELETE_RANGE = "delete_range"
    APPLY_TRANSITION = "apply_transition"
    APPLY_EFFECT = "apply_effect"
    SET_VOLUME = "set_volume"
    ADD_MARKER = "add_marker"
    EXPORT_SEQUENCE = "export_sequence"


class ClipRef(BaseModel):
    """指向时间线上已有的某个片段。

    解析优先级：``ref``（append/insert 时自定义的名字）> ``track`` + ``at``（时间点上覆盖的片段）
    > ``track`` + ``index``（轨道上第 N 个片段，从 0 开始）。
    """

    model_config = ConfigDict(extra="forbid")

    ref: str | None = Field(default=None, description="追加片段时定义的名称，例如 intro")
    track: str | None = Field(default=None, description="轨道名，例如 V1 / A1")
    at: float | None = Field(default=None, description="时间线秒数，命中该时刻的片段")
    index: int | None = Field(default=None, description="轨道内片段序号，从 0 开始")

    def is_empty(self) -> bool:
        return not any(
            value is not None for value in (self.ref, self.track, self.at, self.index)
        )

    def describe(self) -> str:
        parts = []
        if self.ref:
            parts.append(f"ref={self.ref}")
        if self.track:
            parts.append(f"track={self.track}")
        if self.at is not None:
            parts.append(f"at={self.at}")
        if self.index is not None:
            parts.append(f"index={self.index}")
        return ", ".join(parts) or "<empty>"


class OperationBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, description="这条操作的理由，便于人工复核")
    enabled: bool = Field(default=True, description="置为 false 可跳过该操作")


class ImportMediaOp(OperationBase):
    op: Literal[OperationType.IMPORT_MEDIA]
    media: list[str] = Field(
        default_factory=list,
        description="要导入工程的素材：素材 id、文件名或绝对路径；留空表示导入方案里引用到的全部素材",
    )
    bin: str | None = Field(default=None, description="导入到工程面板中的素材箱名称")


class CreateSequenceOp(OperationBase):
    op: Literal[OperationType.CREATE_SEQUENCE]
    name: str = "AI Sequence"
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    preset: str | None = Field(default=None, description="Premiere 序列预设的完整路径，优先于分辨率参数")


class AppendClipOp(OperationBase):
    op: Literal[OperationType.APPEND_CLIP]
    media: str = Field(description="素材 id / 文件名 / 绝对路径")
    ref: str | None = Field(default=None, description="为这个片段起名，后续操作用它引用")
    track: str = Field(default="V1", pattern=TRACK_PATTERN)
    in_point: float = Field(default=0.0, description="源素材入点（秒）")
    out_point: float | None = Field(default=None, description="源素材出点（秒），为空表示到素材结尾")


class InsertClipOp(OperationBase):
    op: Literal[OperationType.INSERT_CLIP]
    media: str
    ref: str | None = None
    track: str = Field(default="V1", pattern=TRACK_PATTERN)
    at: float = Field(default=0.0, description="插入到时间线的哪个时刻（秒）")
    in_point: float = 0.0
    out_point: float | None = None


class MoveClipOp(OperationBase):
    op: Literal[OperationType.MOVE_CLIP]
    clip: ClipRef
    to_time: float = Field(description="移动后的起点（秒）")
    track: str | None = Field(default=None, description="目标轨道，为空表示原轨道")


class TrimClipOp(OperationBase):
    op: Literal[OperationType.TRIM_CLIP]
    clip: ClipRef
    in_point: float = Field(description="修剪后的源入点（秒）")
    out_point: float | None = Field(default=None, description="修剪后的源出点（秒）")


class RazorOp(OperationBase):
    op: Literal[OperationType.RAZOR]
    at: float = Field(description="切割时刻（秒）")
    track: str | None = Field(default=None, description="为空表示所有轨道")


class DeleteRangeOp(OperationBase):
    op: Literal[OperationType.DELETE_RANGE]
    start: float
    end: float
    track: str | None = None
    ripple: bool = Field(default=True, description="删除后是否把后面的素材吸附上来")


class ApplyTransitionOp(OperationBase):
    op: Literal[OperationType.APPLY_TRANSITION]
    at: float = Field(description="转场中心时刻（秒）")
    track: str = Field(default="V1", pattern=TRACK_PATTERN)
    transition: str = Field(default="cross_dissolve", description="cross_dissolve / dip_to_black / wipe ...")
    duration: float = Field(default=1.0, description="转场时长（秒）")


class ApplyEffectOp(OperationBase):
    op: Literal[OperationType.APPLY_EFFECT]
    clip: ClipRef
    effect: str = Field(description="效果名，例如 Lumetri Color / Gaussian Blur")
    params: dict[str, Any] = Field(default_factory=dict)


class SetVolumeOp(OperationBase):
    op: Literal[OperationType.SET_VOLUME]
    clip: ClipRef
    db: float = Field(default=0.0, description="增益，单位 dB，负数为降低音量")


class AddMarkerOp(OperationBase):
    op: Literal[OperationType.ADD_MARKER]
    at: float
    name: str = "marker"
    color: str = "green"
    duration: float = Field(default=0.0, description="标记时长（秒），0 表示点标记")


class ExportSequenceOp(OperationBase):
    op: Literal[OperationType.EXPORT_SEQUENCE]
    output: str = Field(description="输出文件名，相对 workspace/exports 目录")
    preset: str | None = Field(default=None, description="Adobe Media Encoder 预设的完整路径")
    format: str = Field(default="H.264", description="无预设时使用的目标格式标签")


Operation = Annotated[
    Union[
        ImportMediaOp,
        CreateSequenceOp,
        AppendClipOp,
        InsertClipOp,
        MoveClipOp,
        TrimClipOp,
        RazorOp,
        DeleteRangeOp,
        ApplyTransitionOp,
        ApplyEffectOp,
        SetVolumeOp,
        AddMarkerOp,
        ExportSequenceOp,
    ],
    Field(discriminator="op"),
]


class SequenceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "AI Sequence"
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    preset: str | None = None

    @property
    def label(self) -> str:
        return f"{self.width}x{self.height}@{self.fps:g}fps"


class EditPlan(BaseModel):
    """大模型产出的完整剪辑方案。"""

    #: 顶层宽容一些：大模型偶尔会多输出一两个说明字段，不应因此让整个方案失效。
    model_config = ConfigDict(extra="ignore")

    version: str = "1.0"
    project_name: str = Field(default="aiPr_project", description="工程名")
    summary: str = Field(default="", description="用中文简述剪辑思路")
    sequence: SequenceSpec = Field(default_factory=SequenceSpec)
    operations: list[Operation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    source: Literal["llm", "heuristic", "manual"] = "manual"

    @field_validator("operations")
    @classmethod
    def _require_operations(cls, value: list[Operation]) -> list[Operation]:
        if not value:
            raise ValueError("方案必须至少包含一条操作")
        return value

    def enabled_operations(self) -> list[Operation]:
        return [op for op in self.operations if op.enabled]

    def referenced_media(self) -> list[str]:
        keys: list[str] = []
        for op in self.operations:
            media: list[str] = []
            if isinstance(op, ImportMediaOp):
                media = list(op.media)
            elif isinstance(op, (AppendClipOp, InsertClipOp)):
                media = [op.media]
            for item in media:
                if item not in keys:
                    keys.append(item)
        return keys


def plan_json_schema() -> dict[str, Any]:
    """给大模型看的 JSON Schema。"""

    return EditPlan.model_json_schema()
