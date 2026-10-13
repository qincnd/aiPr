"""Premiere 适配器接口。

框架把「怎么控制 Premiere」这件事收敛到一个接口上：

* :class:`~app.services.premiere.mock.MockPremiereAdapter` —— 纯 Python 模拟时间线，
  无需安装 Premiere，用来跑通/回归整条链路，并输出可导入 PR 的 EDL。
* :class:`~app.services.premiere.pymiere_adapter.PymierePremiereAdapter` —— 通过
  `pymiere <https://github.com/qmasingarbe/pymiere>`_ 真实驱动本机的 Premiere Pro。

想接入别的方案（自研 CEP 面板、UXP 插件、生成 .prproj/FCPXML 等），
只要实现同一个 :class:`PremiereAdapter`，并在 ``factory.py`` 里注册即可。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, Field

from app.models.api import BackendInfo
from app.models.media import MediaAsset
from app.models.plan import SequenceSpec
from app.models.timeline import ClipHandle, TimelineState


class PremiereStatus(BaseModel):
    backend: str
    available: bool
    connected: bool
    message: str
    project_name: str | None = None
    version: str | None = None
    capabilities: dict[str, str] = Field(default_factory=dict)

    def to_backend_info(self) -> BackendInfo:
        return BackendInfo(
            backend=self.backend,
            available=self.available,
            connected=self.connected,
            message=self.message,
            capabilities=dict(self.capabilities),
        )


class PremiereAdapter(ABC):
    """所有 Premiere 后端的统一契约。"""

    name: str = "base"
    #: 读起来更友好的中文描述
    description: str = ""

    # ---------------------------------------------------------------- 生命周期
    @abstractmethod
    def status(self) -> PremiereStatus:
        """探测后端是否可用/已连接。"""

    def connect(self) -> None:
        """建立连接；Mock 后端为空实现。"""

    def open_project(self, path: Path | None) -> None:
        """打开或新建工程。``path`` 为空时新建。"""

    def save(self) -> Path | None:
        """保存工程，返回工程文件路径（如果后端有真实文件）。"""
        return None

    def close(self) -> None:
        """释放资源。"""

    # ---------------------------------------------------------------- 素材
    @abstractmethod
    def import_media(self, assets: list[MediaAsset], bin_name: str | None = None) -> list[str]:
        """把素材导入工程，返回导入后的工程面板条目名。"""

    # ---------------------------------------------------------------- 序列
    @abstractmethod
    def create_sequence(
        self,
        spec: SequenceSpec,
        reference_assets: list[MediaAsset] | None = None,
    ) -> TimelineState:
        """新建序列。

        ``reference_assets`` 是方案里会用到、但可能还没导入的素材：真实后端可以拿其中一个视频
        作为规格参照来建序列（这样建出来的序列天然匹配素材，而且不需要用户点任何对话框）。
        """

    # ---------------------------------------------------------------- 时间线编辑
    @abstractmethod
    def append_clip(
        self,
        asset: MediaAsset,
        *,
        ref: str,
        track: str,
        in_point: float,
        out_point: float | None,
    ) -> ClipHandle:
        """在轨道末尾追加片段。"""

    @abstractmethod
    def insert_clip(
        self,
        asset: MediaAsset,
        *,
        ref: str,
        track: str,
        at: float,
        in_point: float,
        out_point: float | None,
    ) -> ClipHandle:
        """在时间线指定位置插入片段（后续片段右移）。"""

    @abstractmethod
    def move_clip(self, handle: ClipHandle, *, to_time: float, track: str | None) -> ClipHandle:
        ...

    @abstractmethod
    def trim_clip(self, handle: ClipHandle, *, in_point: float, out_point: float | None) -> ClipHandle:
        ...

    @abstractmethod
    def razor(self, *, at: float, track: str | None) -> None:
        """在 ``at`` 处切割。``track`` 为空表示所有轨道。"""

    @abstractmethod
    def delete_range(self, *, start: float, end: float, track: str | None, ripple: bool) -> None:
        ...

    @abstractmethod
    def apply_transition(
        self, *, at: float, track: str, transition: str, duration: float
    ) -> None:
        ...

    @abstractmethod
    def apply_effect(self, handle: ClipHandle, *, effect: str, params: dict) -> None:
        ...

    @abstractmethod
    def set_volume(self, handle: ClipHandle, *, db: float) -> None:
        ...

    @abstractmethod
    def add_marker(self, *, at: float, name: str, color: str, duration: float) -> None:
        ...

    # ---------------------------------------------------------------- 导出
    @abstractmethod
    def export_sequence(self, *, output: Path, preset: str | None, format: str) -> list[Path]:
        """导出成片，返回产出的文件列表。"""

    # ---------------------------------------------------------------- 观测
    def snapshot(self) -> TimelineState | None:
        """返回当前时间线快照（Mock 后端支持；真实 PR 后端尽力而为）。"""
        return None

    def capabilities(self) -> dict[str, str]:
        return {}
