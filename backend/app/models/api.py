"""HTTP 接口的请求/响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.job import JobRecord, JobSummary
from app.models.media import MediaLibrary
from app.models.plan import EditPlan


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="要扫描的素材目录，必须在配置的素材根目录内")
    recursive: bool = True
    probe: bool = Field(default=True, description="是否调用 ffprobe 读取时长/分辨率（未安装时自动降级）")


class PlanOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    use_llm: bool = Field(default=True, description="false 时使用内置的启发式排片逻辑，不调用大模型")
    target_duration: float | None = Field(default=None, description="目标成片时长（秒）")
    max_clips: int = Field(default=20, ge=1, le=200)
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    preset: str | None = Field(default=None, description="Premiere 序列预设路径")
    extra_instructions: str = Field(default="", description="额外的风格要求，会拼接到大模型提示词里")


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, description="自然语言剪辑需求")
    media_root: str = Field(description="素材目录")
    media_ids: list[str] = Field(default_factory=list, description="只允许大模型使用这些素材，留空表示全部")
    options: PlanOptions = Field(default_factory=PlanOptions)


class PlanResponse(BaseModel):
    plan: EditPlan
    library: MediaLibrary
    planner: Literal["llm", "heuristic"]
    warnings: list[str] = Field(default_factory=list)


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str | None = Field(default=None, description="直接给提示词，服务端会先生成方案再执行")
    plan: EditPlan | None = Field(default=None, description="直接提交一份现成的方案")
    media_root: str | None = None
    media_ids: list[str] = Field(default_factory=list)
    options: PlanOptions = Field(default_factory=PlanOptions)
    dry_run: bool = Field(default=False, description="只做预演，不写工程/不导出（仍然会调用适配器）")
    continue_on_error: bool = True
    auto_export: bool = Field(default=False, description="方案里没有 export_sequence 时自动追加一次导出")


class JobListResponse(BaseModel):
    jobs: list[JobSummary]


class JobDetailResponse(BaseModel):
    job: JobRecord


class MediaScanResponse(BaseModel):
    library: MediaLibrary
    ffprobe_available: bool


class BackendInfo(BaseModel):
    backend: str
    available: bool
    connected: bool
    message: str
    #: 操作名 -> 支持程度（native / simulated / partial）
    capabilities: dict[str, str] = Field(default_factory=dict)


class SystemInfo(BaseModel):
    app_name: str
    version: str
    python_version: str
    premiere: BackendInfo
    llm_enabled: bool
    llm_configured: bool
    llm_model: str
    llm_base_url: str
    ffprobe_available: bool
    ffprobe_path: str | None = None
    media_roots: list[str]
    workspace_dir: str
