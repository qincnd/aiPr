"""应用配置。

所有字段都可以通过环境变量（前缀 ``AIPR_``）或 ``backend/.env`` 覆盖。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import MediaAccessError

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = PROJECT_ROOT / "backend"


def _default_media_roots() -> str:
    candidates = [
        PROJECT_ROOT / "media",
        Path.home() / "Videos",
        Path.home() / "Desktop",
        Path.home() / "Downloads",
    ]
    return os.pathsep.join(str(p) for p in candidates)


class Settings(BaseSettings):
    """运行时配置。"""

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        env_prefix="AIPR_",
        extra="ignore",
    )

    app_name: str = "aiPr"
    debug: bool = False
    host: str = "127.0.0.1"
    port: int = 8000

    #: 允许的前端来源，逗号分隔（Vite dev server 默认 5173）。
    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:4173,http://127.0.0.1:4173"
    )

    #: 任务产物（方案、日志、时间线快照、导出文件）的根目录。
    workspace_dir: Path = PROJECT_ROOT / "workspace"
    #: 允许扫描/导入的素材根目录，使用系统路径分隔符拼接（Windows 为 `;`）。
    media_roots: str = Field(default_factory=_default_media_roots)
    max_media_files: int = 2000

    #: ffprobe 可执行文件路径，留空则从 PATH 中自动查找。
    ffprobe_path: str | None = None

    #: 控制 Premiere 的后端实现：mock（无需安装 PR）或 pymiere（真实控制）。
    premiere_backend: Literal["mock", "pymiere"] = "mock"
    #: 可选的既有工程文件，执行方案前会先打开它。
    premiere_project_path: Path | None = None

    llm_enabled: bool = True
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str | None = None
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: float = 120.0
    llm_temperature: float = 0.2

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def media_root_paths(self) -> list[Path]:
        raw = [item.strip() for item in self.media_roots.split(os.pathsep) if item.strip()]
        return [Path(item).expanduser() for item in raw]

    @property
    def runs_dir(self) -> Path:
        return self.workspace_dir / "runs"

    @property
    def exports_dir(self) -> Path:
        return self.workspace_dir / "exports"

    @property
    def llm_available(self) -> bool:
        return self.llm_enabled and bool(self.llm_api_key)

    def model_post_init(self, __context: Any) -> None:
        self.workspace_dir = Path(self.workspace_dir).expanduser()
        if self.premiere_project_path is not None:
            self.premiere_project_path = Path(self.premiere_project_path).expanduser()

    def prepare_dirs(self) -> None:
        for directory in (self.workspace_dir, self.runs_dir, self.exports_dir):
            directory.mkdir(parents=True, exist_ok=True)


def is_within(path: Path, root: Path) -> bool:
    """判断 ``path`` 是否位于 ``root`` 之内（解析符号链接后）。"""

    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def assert_allowed_media_path(raw_path: str | Path, roots: list[Path]) -> Path:
    """校验素材路径位于允许的根目录内，返回解析后的绝对路径。"""

    try:
        candidate = Path(raw_path).expanduser().resolve()
    except OSError as exc:  # pragma: no cover - 取决于平台
        raise MediaAccessError(f"无法解析路径：{raw_path}（{exc}）") from exc

    if not candidate.exists():
        raise MediaAccessError(f"路径不存在：{candidate}")

    if not any(is_within(candidate, root) for root in roots):
        allowed = ", ".join(str(root) for root in roots)
        raise MediaAccessError(f"路径 {candidate} 不在允许的素材根目录内。允许的根目录：{allowed}")

    return candidate


def assert_allowed_output_path(raw_path: str | Path, exports_dir: Path) -> Path:
    """校验导出路径必须落在工作区的导出目录内。"""

    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = exports_dir / candidate
    candidate = candidate.resolve()

    if not is_within(candidate, exports_dir):
        raise MediaAccessError(f"导出路径必须位于 {exports_dir} 之内，收到：{candidate}")

    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
