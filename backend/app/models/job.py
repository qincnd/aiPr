"""任务（Job）数据模型。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from app.models.plan import EditPlan
from app.models.timeline import TimelineState


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class JobEvent(BaseModel):
    seq: int
    at: datetime = Field(default_factory=utc_now)
    level: EventLevel = EventLevel.INFO
    message: str
    op_index: int | None = None
    op: str | None = None
    data: dict = Field(default_factory=dict)


class JobRecord(BaseModel):
    id: str = Field(default_factory=new_job_id)
    status: JobStatus = JobStatus.QUEUED
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    media_root: str | None = None
    dry_run: bool = False
    continue_on_error: bool = True
    plan: EditPlan | None = None
    progress: float = 0.0
    events: list[JobEvent] = Field(default_factory=list)
    error: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    timeline: TimelineState | None = None
    run_dir: str | None = None

    @property
    def operation_count(self) -> int:
        return len(self.plan.operations) if self.plan else 0

    def duration_seconds(self) -> float | None:
        if not self.started_at or not self.finished_at:
            return None
        return round((self.finished_at - self.started_at).total_seconds(), 3)


class JobSummary(BaseModel):
    """列表接口用的精简视图。"""

    id: str
    status: JobStatus
    created_at: datetime
    media_root: str | None = None
    project_name: str | None = None
    progress: float = 0.0
    operation_count: int = 0
    error: str | None = None

    @classmethod
    def from_record(cls, record: JobRecord) -> "JobSummary":
        return cls(
            id=record.id,
            status=record.status,
            created_at=record.created_at,
            media_root=record.media_root,
            project_name=record.plan.project_name if record.plan else None,
            progress=record.progress,
            operation_count=record.operation_count,
            error=record.error,
        )
