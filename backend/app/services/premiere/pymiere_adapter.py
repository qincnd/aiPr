"""真实驱动本机 Adobe Premiere Pro 的适配器（pymiere + 本仓库的 CEP 桥接）。

链路：Python(pymiere) --HTTP--> CEP 面板(premiere-bridge/com.aipr.premiere.link)
      --evalScript--> Premiere Pro 的 ExtendScript 引擎

环境要求（缺一不可）：
1. 本机已安装 Adobe Premiere Pro（本仓库在 Premiere 2026 / 26.0 上验证）；
2. ``pip install -r backend/requirements-premiere.txt``（含 pymiere 与 setuptools）；
3. 已安装并打开 CEP 面板：``scripts/install-premiere-bridge.ps1`` 安装，
   然后在 Premiere 里打开【窗口 > 扩展 > aiPr Link】；
4. 连通性自检：``python scripts/check_premiere.py``。

与 pymiere/Premiere 打交道的几条硬性约束（都是踩过的坑，别改回去）：

* ``clip.start / end / inPoint / outPoint`` 读出来是 **Time 对象**，必须取 ``.seconds``；
  写进去也必须传 ``Time``（用 ``wrappers.time_from_seconds``）。
* ``project.importFiles(paths, suppressUI, targetBin, asNumberedStills)`` 做了**严格类型检查**，
  ``targetBin`` 不能传 None；它返回数组，而 pymiere 不支持数组返回值，
  所以导入后一律**按 mediaPath 回查** ProjectItem。
* ``project.createNewSequence(name, presetPath)`` 的第二个参数必须是**真实存在的 .sqpreset 路径**
  （传空串会弹新建序列对话框）；没有素材可参照时，这里按分辨率/帧率自动挑一个系统预设。
* 跨轨道移动片段在 ExtendScript API 里做不到（只有 QE 能做），因此 ``move_clip`` 忽略 track 参数并告警。
* 转场 / 效果 / 切割只能走 QE（该 API 在不同 PR 版本上签名不一致），属于尽力而为，
  ``capabilities()`` 里标成 partial，失败时抛出带原因的 PremiereError。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.errors import PremiereError, PremiereUnavailableError
from app.core.logging import get_logger
from app.models.media import MediaAsset, MediaKind
from app.models.plan import SequenceSpec
from app.models.timeline import ClipHandle, TimelineClip, TimelineMarker, TimelineState, TimelineTrack

from .base import PremiereAdapter, PremiereStatus

logger = get_logger(__name__)

#: 目标帧率 -> 序列预设文件名里的帧率记号（Premiere 用的是 29.97 / 23.976 / 59.94 这类写法）
FPS_TOKENS: dict[float, str] = {
    23.976: "23.976",
    24.0: "23.976",
    25.0: "25",
    29.97: "29.97",
    30.0: "29.97",
    50.0: "50",
    59.94: "59.94",
    60.0: "59.94",
}

TRANSITION_NAMES = {
    "cross_dissolve": "Cross Dissolve",
    "dip_to_black": "Dip to Black",
    "dip_to_white": "Dip to White",
    "wipe": "Wipe",
    "slide": "Slide",
    "page_peel": "Page Peel",
}

PORTRAIT_TOKENS = ("portrait", "9x16", "4x5", "vertical")
SQUARE_TOKENS = ("square", "1x1")
NOISY_TOKENS = ("hdr", "proxy", "anamorphic", "vr", "360", "mono", "multicam", "trial", "legacy", "hdv")

PROJECT_ITEM_TYPE_BIN = 2
MIN_CLIP_SECONDS = 0.05

INSTALL_HINT = (
    "无法连接 Premiere Pro。请依次确认："
    "1) Premiere Pro 已启动；"
    "2) 已执行 pip install -r backend/requirements-premiere.txt；"
    "3) 已执行 scripts/install-premiere-bridge.ps1 安装 CEP 面板；"
    "4) 在 Premiere 里打开了【窗口 > 扩展 > aiPr Link】面板；"
    "5) 运行 python scripts/check_premiere.py 自检。"
)


class PymierePremiereAdapter(PremiereAdapter):
    name = "pymiere"
    description = "通过 pymiere + aiPr Link 面板真实驱动本机 Premiere Pro"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._pymiere: Any = None
        self._wrappers: Any = None
        self._sequence: Any = None
        self._item_cache: dict[str, Any] = {}
        self._clip_registry: dict[str, Any] = {}
        self._handles: dict[str, ClipHandle] = {}
        self._imported_assets: dict[str, MediaAsset] = {}

    # ---------------------------------------------------------------- 连接
    def _lib(self) -> Any:
        if self._pymiere is not None:
            return self._pymiere
        try:
            import pymiere
        except ImportError as exc:  # pragma: no cover - 取决于本机环境
            raise PremiereUnavailableError(
                "pymiere 导入失败：{}。请执行 pip install -r backend/requirements-premiere.txt"
                "（pymiere 依赖 Python 3.12 起已移除的 distutils，requirements 里带了 setuptools 兼容层）".format(exc)
            ) from exc
        try:
            _ = pymiere.objects.app.version
        except Exception as exc:  # pragma: no cover
            raise PremiereUnavailableError(f"{INSTALL_HINT}（{exc}）") from exc

        # pymiere 的对象在 GC 时会回调 Premiere；解释器退出阶段这些回调只会刷一屏
        # "sys.meta_path is None" 噪音（甚至卡住退出），这里提前把它变成空操作。
        import atexit  # noqa: PLC0415

        from pymiere import core as pymiere_core  # noqa: PLC0415

        atexit.register(setattr, pymiere_core, "eval_script", lambda *args, **kwargs: None)

        self._pymiere = pymiere
        return pymiere

    def _wr(self) -> Any:
        """pymiere.wrappers —— 时间对象、移动片段、系统预设等现成工具。"""

        if self._wrappers is None:
            self._lib()
            try:
                from pymiere import wrappers
            except Exception as exc:  # pragma: no cover
                raise PremiereUnavailableError(f"pymiere.wrappers 不可用：{exc}") from exc
            self._wrappers = wrappers
        return self._wrappers

    # ---------------------------------------------------------------- 状态
    def status(self) -> PremiereStatus:
        try:
            lib = self._lib()
            version = str(lib.objects.app.version)
            project = lib.objects.app.project
            project_name = str(getattr(project, "name", "") or "") or None
            active = getattr(project, "activeSequence", None)
            sequence_name = str(getattr(active, "name", "") or "") if active is not None else None
            message = f"已连接 Premiere Pro {version}"
            if sequence_name:
                message += f"（当前序列：{sequence_name}）"
            return PremiereStatus(
                backend=self.name,
                available=True,
                connected=True,
                message=message,
                project_name=project_name,
                version=version,
                capabilities=self.capabilities(),
            )
        except PremiereUnavailableError as exc:
            return PremiereStatus(
                backend=self.name,
                available=False,
                connected=False,
                message=str(exc),
                capabilities=self.capabilities(),
            )
        except Exception as exc:  # pragma: no cover
            return PremiereStatus(
                backend=self.name,
                available=False,
                connected=False,
                message=f"Premiere 状态异常：{exc}",
                capabilities=self.capabilities(),
            )

    def capabilities(self) -> dict[str, str]:
        return {
            "import_media": "native",
            "create_sequence": "native",
            "append_clip": "native",
            "insert_clip": "native",
            "move_clip": "partial",
            "trim_clip": "native",
            "razor": "partial",
            "delete_range": "partial",
            "apply_transition": "partial",
            "apply_effect": "partial",
            "set_volume": "partial",
            "add_marker": "native",
            "export_sequence": "native",
        }

    def connect(self) -> None:
        self._lib()

    def close(self) -> None:
        self._sequence = None
        self._item_cache.clear()
        self._clip_registry.clear()
        self._handles.clear()
        self._imported_assets.clear()

    # ---------------------------------------------------------------- 时间工具
    def _time(self, seconds: float) -> Any:
        """构造 ExtendScript 的 Time 对象（写 inPoint/start 等属性时必须用它）。"""

        return self._wr().time_from_seconds(float(seconds))

    @staticmethod
    def _seconds(value: Any, default: float = 0.0) -> float:
        if value is None:
            return float(default)
        seconds = getattr(value, "seconds", None)
        if seconds is not None:
            try:
                return float(seconds)
            except (TypeError, ValueError):
                return float(default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _eval(self, script: str, timeout: float = 30.0) -> Any:
        """直接执行一段 ExtendScript。

        刻意不用 pymiere.core.eval_script：它没设超时，一旦 Premiere 弹出对话框挡住脚本执行，
        整个任务就会永久挂住（实测踩过）。这里用带超时的 POST，超时后给出可操作的错误提示。
        """

        self._lib()
        import requests  # noqa: PLC0415 - 懒加载

        from pymiere import core as pymiere_core  # noqa: PLC0415

        code = script.replace("\\", "\\\\")
        payload = {
            "to_eval": "try{\n" + code + "\n}catch(e){e.error=true;ExtendJSON.stringify(e)}"
        }
        try:
            response = requests.post(pymiere_core.PANEL_URL, json=payload, timeout=timeout)
        except requests.exceptions.Timeout as exc:
            raise PremiereError(
                f"Premiere 在 {timeout:.0f} 秒内没有响应——通常是界面弹了对话框挡住了脚本执行，"
                "请切到 Premiere 窗口关掉对话框后重试"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise PremiereUnavailableError(f"{INSTALL_HINT}（{exc}）") from exc

        text = response.content.decode("utf-8", errors="replace")
        try:
            decoded = json.loads(text)
        except ValueError:
            return text
        if isinstance(decoded, dict) and decoded.get("error") is True:
            raise pymiere_core.ExtendScriptError(decoded)
        return decoded

    @staticmethod
    def _escaped(text: str) -> str:
        """把字符串变成可直接塞进 ExtendScript 源码的字面量。"""

        return json.dumps(str(text), ensure_ascii=False)

    # ---------------------------------------------------------------- 工程
    def _project(self) -> Any:
        lib = self._lib()
        try:
            project = lib.objects.app.project
        except Exception as exc:  # pragma: no cover
            raise PremiereUnavailableError(f"{INSTALL_HINT}（{exc}）") from exc
        if project is None:
            raise PremiereError("Premiere 里没有打开的工程，请先新建/打开一个项目")
        return project

    def open_project(self, path: Path | None) -> None:
        if path is None:
            logger.info("沿用 Premiere 当前打开的工程")
            return
        project_path = Path(path)
        if not project_path.is_file():
            raise PremiereError(f"工程文件不存在：{project_path}")
        try:
            self._eval(f"app.openDocument({self._escaped(str(project_path))});")
        except PremiereUnavailableError:
            raise
        except Exception as exc:  # pragma: no cover
            raise PremiereError(f"打开工程失败：{project_path}（{exc}）") from exc
        logger.info("已请求 Premiere 打开工程：%s", project_path)

    def save(self) -> Path | None:
        try:
            self._project().save()
        except PremiereError:
            raise
        except Exception as exc:  # pragma: no cover
            raise PremiereError(f"保存工程失败：{exc}") from exc
        configured = self.settings.premiere_project_path
        return Path(configured) if configured else None

    # ---------------------------------------------------------------- 序列
    def _sequence_or_fail(self) -> Any:
        if self._sequence is not None:
            return self._sequence
        active = getattr(self._project(), "activeSequence", None)
        if active is None:
            raise PremiereError("Premiere 里没有活动序列，请先执行 create_sequence")
        self._sequence = active
        return active

    def create_sequence(
        self,
        spec: SequenceSpec,
        reference_assets: list[MediaAsset] | None = None,
    ) -> TimelineState:
        """新建序列。

        真机教训（Premiere 2026 实测）：``project.createNewSequence(name, presetPath)`` 会弹
        「新建序列」对话框，而对话框会把后续所有 ExtendScript 调用永久堵死。所以默认路径刻意避开它：
        拿一个素材（已导入的，或方案里引用到的）用 ``createNewSequenceFromClips`` 建序列——
        这个接口不弹窗，且序列规格天然匹配素材。只有方案显式给了 preset 才走预设路径。
        """

        project = self._project()
        self._sequence = None
        try:
            reference = self._first_video_item() or self._import_reference(reference_assets or [])
            if reference is not None:
                logger.info("按素材规格新建序列：%s", spec.name)
                self._sequence = project.createNewSequenceFromClips(spec.name, [reference])
                self._clear_sequence(self._sequence)
                self._rename_sequence(self._sequence, spec.name)
            elif spec.preset:
                logger.info("按方案指定的预设新建序列：%s", spec.preset)
                self._create_from_preset(spec)
            else:
                raise PremiereError(
                    "无法新建序列：方案里既没有可参照的素材，也没有指定序列预设。"
                    "请在方案里加上 import_media/append_clip（引用素材），或给 create_sequence 填 preset"
                )
        except PremiereError:
            raise
        except Exception as exc:  # pragma: no cover
            raise PremiereError(f"新建序列失败（{spec.name}）：{exc}") from exc

        if self._sequence is None:
            raise PremiereError(f"新建序列失败：Premiere 没有返回名为「{spec.name}」的序列")
        logger.info("已新建序列：%s", spec.name)

        timeline = self._read_timeline() or TimelineState(sequence_name=spec.name)
        self._warn_on_spec_mismatch(spec, timeline)
        return timeline

    def _create_from_preset(self, spec: SequenceSpec) -> None:
        """按预设建序列。

        ``createNewSequence`` 走的是「可能弹窗」的接口，所以这里用带超时的 ``_eval`` 直接发，
        一旦卡住就抛出可操作的错误，而不是把整个任务挂死。
        """

        preset = self._resolve_sequence_preset(spec)
        logger.info("使用序列预设：%s", preset)
        self._eval(
            f"app.project.createNewSequence({self._escaped(spec.name)}, {self._escaped(preset)});",
            timeout=20.0,
        )
        self._sequence = self._find_sequence(spec.name)

    def _import_reference(self, assets: list[MediaAsset]) -> Any:
        """没有已导入素材时，按方案引用把一个视频素材导进来，用作建序列的规格参照。"""

        for asset in assets:
            if asset.kind is not MediaKind.VIDEO:
                continue
            logger.info("建序列前先导入规格参照素材：%s", asset.name)
            self.import_media([asset])
            item = self._item_cache.get(asset.id)
            if item is not None:
                return item
        return None

    def _first_video_item(self) -> Any:
        """挑一个已导入的视频素材作为建序列的规格参照。"""

        for asset_id, asset in self._imported_assets.items():
            if asset.kind.value != "video":
                continue
            item = self._item_cache.get(asset_id)
            if item is not None:
                return item
        for item in self._item_cache.values():
            if getattr(item, "type", None) == 1:
                return item
        return None

    def _rename_sequence(self, sequence: Any, name: str) -> None:
        try:
            sequence.name = name
        except Exception as exc:  # noqa: BLE001
            logger.warning("重命名序列失败（当前名 %s）：%s", name, exc)

    def _warn_on_spec_mismatch(self, spec: SequenceSpec, timeline: TimelineState) -> None:
        if not timeline.width or not timeline.height:
            return
        if timeline.width != spec.width or timeline.height != spec.height:
            logger.warning(
                "序列规格与方案不一致：实际 %sx%s，方案要求 %sx%s（按素材规格建序列时这是正常的；"
                "要强制指定可在 create_sequence 里填 preset）",
                timeline.width,
                timeline.height,
                spec.width,
                spec.height,
            )
        elif abs(timeline.fps - spec.fps) > 0.05:
            logger.warning("序列帧率与方案不一致：实际 %gfps，方案要求 %gfps", timeline.fps, spec.fps)

    def _clear_sequence(self, sequence: Any) -> None:
        """createNewSequenceFromClips 会把素材真的插进序列，这里清空以保证「新建即空」的语义。"""

        for collection in (sequence.videoTracks, sequence.audioTracks):
            for index in range(int(collection.numTracks)):
                for clip in self._clips(collection[index]):
                    try:
                        clip.remove(False, True)
                    except Exception:  # pragma: no cover
                        clip.remove()

    def _find_sequence(self, name: str) -> Any:
        project = self._project()
        try:
            for sequence in project.sequences:
                if str(getattr(sequence, "name", "")) == name:
                    return sequence
        except Exception as exc:  # pragma: no cover
            logger.debug("按名字查找序列失败：%s", exc)
        return getattr(project, "activeSequence", None)

    def _presets_root(self) -> Path:
        try:
            app_path = self._lib().objects.app.path
        except Exception as exc:  # pragma: no cover
            raise PremiereError(f"拿不到 Premiere 安装路径：{exc}") from exc
        return Path(str(app_path)) / "Settings" / "SequencePresets"

    def _resolve_sequence_preset(self, spec: SequenceSpec) -> str:
        """按分辨率 + 帧率挑一个系统自带的 .sqpreset。

        预设目录名在不同大版本里会变（2019 是 Digital SLR，2026 是 HD 1080p / UHD (4K) / Social），
        所以这里不写死目录，而是扫描全部预设文件名再打分挑最合适的。
        """

        if spec.preset:
            preset_path = Path(spec.preset)
            if not preset_path.is_file():
                raise PremiereError(f"方案里指定的序列预设不存在：{preset_path}")
            return str(preset_path)

        root = self._presets_root()
        if not root.is_dir():
            raise PremiereError(
                f"找不到序列预设目录：{root}（可在方案里用 preset 字段指定 .sqpreset 的完整路径）"
            )

        fps_tokens = self._fps_tokens(spec.fps)
        if spec.height > spec.width:
            orientation = "portrait"
        elif spec.height == spec.width:
            orientation = "square"
        else:
            orientation = "landscape"

        best: tuple[int, int, Path] | None = None
        available: list[str] = []
        for candidate in sorted(root.rglob("*.sqpreset")):
            text = str(candidate.relative_to(root)).lower()
            available.append(text)
            if not any(token in text for token in fps_tokens):
                continue
            score = 0
            if str(spec.height) in text:
                score += 4
            if str(spec.width) in text:
                score += 2
            if orientation == "portrait":
                score += 6 if any(token in text for token in PORTRAIT_TOKENS) else -8
            elif orientation == "square":
                score += 6 if any(token in text for token in SQUARE_TOKENS) else -8
            elif any(token in text for token in PORTRAIT_TOKENS + SQUARE_TOKENS):
                score -= 8
            score -= 3 * sum(1 for token in NOISY_TOKENS if token in text)
            # 同等条件下更具体的名字更可靠（"HD 1080p 25 fps" 优于 "HDV 1080p25"）
            entry = (score, len(text), candidate)
            if best is None or entry[:2] > best[:2]:
                best = entry

        if best is None:
            preview = "、".join(available[:8]) or "（目录为空）"
            raise PremiereError(
                f"在 {root} 里找不到 {spec.width}x{spec.height}@{spec.fps:g}fps 匹配的序列预设。"
                f"可选项示例：{preview}"
            )
        logger.debug("序列预设打分结果：%s -> %s", spec.label, best[2].name)
        return str(best[2])

    @staticmethod
    def _fps_tokens(fps: float) -> tuple[str, ...]:
        """能代表该帧率的文件名记号。

        Premiere 的预设文件名里 30 写成 "29.97"、24 写成 "23.976"，但 Social 目录又直接写 "30 fps"，
        所以两种都要接受。
        """

        rounded = round(float(fps), 3)
        tokens = [FPS_TOKENS.get(rounded, ""), f"{rounded:g}"]
        integer = int(rounded)
        if abs(rounded - integer) < 0.05:
            tokens.append(str(integer))
        return tuple(dict.fromkeys(token for token in tokens if token))

    # ---------------------------------------------------------------- 素材
    def import_media(self, assets: list[MediaAsset], bin_name: str | None = None) -> list[str]:
        if not assets:
            return []
        project = self._project()
        paths = [str(Path(asset.path)) for asset in assets]
        try:
            # 刻意不走 pymiere 的 project.importFiles()：
            # 1) 它的字符串格式化会生成 """path"""（ExtendScript 不认）；
            # 2) 它的 targetBin 参数做了严格类型检查，传 None 会直接报错；
            # 3) Project 上也没有包 createBin。
            # 这里直接发原生的 ExtendScript，路径用自己的 JSON 转义，最稳。
            self._eval(self._import_script(paths, bin_name))
        except PremiereUnavailableError:
            raise
        except Exception as exc:  # pragma: no cover
            raise PremiereError(f"导入素材失败：{exc}") from exc

        names: list[str] = []
        for asset in assets:
            item = self._find_item_by_media_path(asset.path)
            if item is not None:
                self._item_cache[asset.id] = item
                self._imported_assets[asset.id] = asset
                names.append(str(getattr(item, "name", asset.name)))
            else:
                logger.warning("导入后没有在工程里找到素材：%s", asset.path)
                names.append(asset.name)
        logger.info("已导入 %d 个素材", len(names))
        return names

    def _import_script(self, paths: list[str], bin_name: str | None) -> str:
        paths_literal = "[" + ", ".join(self._escaped(path) for path in paths) + "]"
        if bin_name:
            name_literal = self._escaped(bin_name)
            setup = (
                "var _aiprRoot = app.project.rootItem;"
                "var _aiprBin = null;"
                "for (var _i = 0; _i < _aiprRoot.children.numItems; _i++) {"
                "  var _c = _aiprRoot.children[_i];"
                f"  if (_c.type === 2 && _c.name === {name_literal}) {{ _aiprBin = _c; break; }}"
                "}"
                # 真机实测：bin 要由根素材箱创建，app.project 上没有 createBin
                f"if (_aiprBin === null) {{ _aiprBin = _aiprRoot.createBin({name_literal}); }}"
                "if (_aiprBin === null) { _aiprBin = _aiprRoot; }"
            )
        else:
            setup = "var _aiprBin = app.project.rootItem;"
        return f"{setup} app.project.importFiles({paths_literal}, true, _aiprBin, false);"

    def _resolve_bin(self, bin_name: str | None) -> Any:
        """查找同名素材箱（导入本身走 _import_script，这里供需要时定位素材箱用）。"""

        root = self._project().rootItem
        if not bin_name:
            return root
        for item in self._iter_project_items():
            if (
                str(getattr(item, "name", "")).lower() == bin_name.lower()
                and getattr(item, "type", None) == PROJECT_ITEM_TYPE_BIN
            ):
                return item
        return root

    def _iter_project_items(self) -> list[Any]:
        try:
            return list(self._wr().get_item_recursive(self._project().rootItem))
        except Exception as exc:  # pragma: no cover
            logger.debug("遍历工程面板失败：%s", exc)
            return []

    def _find_item_by_media_path(self, media_path: str) -> Any:
        wanted = self._normalize_path(media_path)
        wanted_name = Path(media_path).name.lower()
        for item in self._iter_project_items():
            try:
                item_path = item.getMediaPath()
            except Exception:  # noqa: BLE001 - 素材箱没有 mediaPath
                item_path = None
            if item_path and self._normalize_path(item_path) == wanted:
                return item
            if str(getattr(item, "name", "")).lower() == wanted_name:
                return item
        return None

    @staticmethod
    def _normalize_path(value: str) -> str:
        return str(Path(str(value))).replace("\\", "/").lower()

    def _project_item(self, asset: MediaAsset) -> Any:
        item = self._item_cache.get(asset.id)
        if item is None:
            item = self._find_item_by_media_path(asset.path)
        if item is None:
            self.import_media([asset])
            item = self._item_cache.get(asset.id)
        if item is None:
            raise PremiereError(f"工程面板里找不到素材：{asset.name}（{asset.path}）")
        return item

    # ---------------------------------------------------------------- 轨道
    def _track(self, track_name: str) -> Any:
        sequence = self._sequence_or_fail()
        upper = track_name.upper()
        digits = upper[1:] if upper[1:].isdigit() else "1"
        index = int(digits) - 1
        collection = sequence.videoTracks if upper.startswith("V") else sequence.audioTracks
        count = int(collection.numTracks)
        if index < 0 or index >= count:
            raise PremiereError(
                f"序列里没有 {track_name} 轨道（当前视频轨 {sequence.videoTracks.numTracks} 条，"
                f"音频轨 {sequence.audioTracks.numTracks} 条）"
            )
        return collection[index]

    @staticmethod
    def _clips(track: Any) -> list[Any]:
        collection = getattr(track, "clips", None)
        return list(collection) if collection is not None else []

    def _track_end(self, track: Any) -> float:
        return max(
            (self._seconds(getattr(clip, "end", None)) for clip in self._clips(track)),
            default=0.0,
        )

    # ---------------------------------------------------------------- 时间线编辑
    def append_clip(
        self,
        asset: MediaAsset,
        *,
        ref: str,
        track: str,
        in_point: float,
        out_point: float | None,
    ) -> ClipHandle:
        target = self._track(track)
        return self._insert_into(target, asset, ref, track, self._track_end(target), in_point, out_point)

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
        target = self._track(track)
        return self._insert_into(target, asset, ref, track, float(at), in_point, out_point)

    def _insert_into(
        self,
        target: Any,
        asset: MediaAsset,
        ref: str,
        track: str,
        start: float,
        in_point: float,
        out_point: float | None,
    ) -> ClipHandle:
        item = self._project_item(asset)
        try:
            # pymiere 的 Track.insertClip 是无返回值包装（ExtendScript 侧 insertClip 本身也是 void），
            # 所以插完必须自己在轨道上把片段找回来。
            target.insertClip(item, self._time(start))
        except Exception as exc:  # pragma: no cover
            raise PremiereError(f"插入片段失败（{asset.name} → {track}@{start:.2f}s）：{exc}") from exc

        clip = self._find_clip_at(target, start)
        if clip is None:
            raise PremiereError(
                f"插入后在 {track} 轨道上找不到起点位于 {start:.2f}s 的片段（{asset.name}）"
            )

        actual_start = self._seconds(getattr(clip, "start", None), start)
        actual_end = self._apply_window(clip, in_point, out_point, actual_start)
        handle = ClipHandle(
            ref=ref or asset.stem,
            track=track,
            media_ref=asset.id,
            start=actual_start,
            end=actual_end,
            source_in=float(in_point),
            source_out=(
                float(out_point)
                if out_point is not None
                else round(float(in_point) + (actual_end - actual_start), 3)
            ),
        )
        self._register_native(handle.ref, clip, handle)
        return handle

    def _find_clip_at(self, track: Any, start: float, tolerance: float = 0.3) -> Any:
        """按起点时间在轨道上找片段（同一起点有多个时取最后一个，即刚插入的那个）。"""

        found = None
        for clip in self._clips(track):
            if abs(self._seconds(getattr(clip, "start", None)) - float(start)) <= tolerance:
                found = clip
        return found

    def _apply_window(
        self,
        clip: Any,
        in_point: float,
        out_point: float | None,
        start: float,
    ) -> float:
        """把片段裁到 [in_point, out_point)。顺序与 pymiere 官方 edit_clip 一致：end → start → in → out。"""

        if out_point is not None:
            duration = max(MIN_CLIP_SECONDS, float(out_point) - float(in_point))
            self._try_set(clip, "end", start + duration)
        self._try_set(clip, "start", start)
        self._try_set(clip, "inPoint", float(in_point))
        if out_point is not None:
            self._try_set(clip, "outPoint", float(out_point))
        return self._seconds(getattr(clip, "end", None), start)

    def _try_set(self, clip: Any, attribute: str, seconds: float) -> None:
        """Premiere 某些版本对 start/end/inPoint/outPoint 只读或会抛错，逐个兜住，不中断整条方案。"""

        try:
            setattr(clip, attribute, self._time(float(seconds)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("设置片段的 %s 失败（当前版本可能不支持）：%s", attribute, exc)

    def move_clip(self, handle: ClipHandle, *, to_time: float, track: str | None) -> ClipHandle:
        clip = self._native_clip(handle)
        current = self._seconds(getattr(clip, "start", None), handle.start)
        delta = float(to_time) - current
        if abs(delta) > 1e-6:
            try:
                self._wr().move_clip(clip, delta)
            except Exception as exc:  # pragma: no cover
                raise PremiereError(f"移动片段失败（{handle.ref}）：{exc}") from exc
        if track and track != handle.track:
            logger.warning("ExtendScript API 不支持跨轨道移动，%s 仍留在 %s 轨", handle.ref, handle.track)
        start = self._seconds(getattr(clip, "start", None), to_time)
        end = self._seconds(getattr(clip, "end", None), start + (handle.end - handle.start))
        moved = handle.model_copy(update={"start": start, "end": end})
        self._register_native(moved.ref, clip, moved)
        return moved

    def trim_clip(self, handle: ClipHandle, *, in_point: float, out_point: float | None) -> ClipHandle:
        clip = self._native_clip(handle)
        start = self._seconds(getattr(clip, "start", None), handle.start)
        end = self._apply_window(clip, in_point, out_point, start)
        trimmed = handle.model_copy(
            update={
                "start": start,
                "end": end,
                "source_in": float(in_point),
                "source_out": (
                    float(out_point)
                    if out_point is not None
                    else round(float(in_point) + (end - start), 3)
                ),
            }
        )
        self._register_native(trimmed.ref, clip, trimmed)
        return trimmed

    def razor(self, *, at: float, track: str | None) -> None:
        """QE 的 razor 需要时间码字符串。

        注意：QE 必须用**原生 ExtendScript** 调用——pymiere 的 QE 是 PymiereGenericObject，
        它靠 ExtendScript 的 reflect 查方法名，而 QE 对象上查不到 `getVideoTrackAt`（真机实测）。
        """

        sequence = self._sequence_or_fail()
        timecode = self._escaped(self._timecode(at))
        try:
            if track:
                self._eval(f"{self._qe_track_expr(track)}.razor({timecode});", timeout=30.0)
                return
            script = (
                "var _seq = app.project.activeSequence;var _i = 0;"
                f"for (_i = 0; _i < _seq.videoTracks.numTracks; _i++) {{ qe.project.getVideoTrackAt(_i).razor({timecode}); }}"
                f"for (_i = 0; _i < _seq.audioTracks.numTracks; _i++) {{ qe.project.getAudioTrackAt(_i).razor({timecode}); }}"
            )
            self._eval(script, timeout=60.0)
        except Exception as exc:
            raise PremiereError(f"切割失败（{at:.2f}s）：{exc}") from exc
        _ = sequence

    def delete_range(self, *, start: float, end: float, track: str | None, ripple: bool) -> None:
        if end <= start:
            raise PremiereError(f"删除区间非法：{start} → {end}")
        sequence = self._sequence_or_fail()
        track_names = [track] if track else self._all_track_names(sequence)

        for name in track_names:
            for clip in self._clips(self._track(name)):
                clip_start = self._seconds(getattr(clip, "start", None))
                clip_end = self._seconds(getattr(clip, "end", None))
                if clip_end <= start or clip_start >= end:
                    continue
                if clip_start < start and clip_end > end:
                    # 区间落在片段内部：ExtendScript 没有 split，只能借 QE 切两刀
                    self._razor_silently(name, start)
                    self._razor_silently(name, end)
                elif clip_start < start:
                    self._try_set(clip, "end", start)
                else:
                    # 头部越界：把源入点前推相同距离，画面内容才不会跳
                    offset = end - clip_start
                    self._try_set(clip, "inPoint", self._seconds(getattr(clip, "inPoint", None)) + offset)
                    self._try_set(clip, "start", start if ripple else end)

        # 切完再做一轮清理（此时区间边界与片段边界已对齐）
        for name in track_names:
            for clip in self._clips(self._track(name)):
                clip_start = self._seconds(getattr(clip, "start", None))
                clip_end = self._seconds(getattr(clip, "end", None))
                if clip_start >= start - 1e-3 and clip_end <= end + 1e-3:
                    try:
                        clip.remove(bool(ripple), True)
                    except Exception:  # pragma: no cover - 老版本没有 remove
                        clip.remove()

    def _razor_silently(self, track: str, at: float) -> None:
        try:
            self.razor(at=at, track=track)
        except PremiereError as exc:
            logger.warning("区间落在片段内部，但切割失败（%s）：%s", track, exc)

    def apply_transition(self, *, at: float, track: str, transition: str, duration: float) -> None:
        """加转场。

        QE 必须用原生 ExtendScript 调（pymiere 的 QE 泛型对象查不到方法名），
        而且 addTransition 的签名在不同版本间有差异，所以脚本里依次尝试常见顺序。
        """

        preset_name = TRANSITION_NAMES.get(transition, transition)
        name_literal = self._escaped(preset_name)
        start_timecode = self._escaped(self._timecode(max(0.0, at - duration / 2)))
        duration_timecode = self._escaped(self._timecode(duration))
        script = (
            f"var _trans = qe.project.getVideoTransitionByName({name_literal});"
            f"if (!_trans) {{ throw new Error('Premiere 里没有名为 ' + {name_literal} + ' 的转场'); }}"
            f"var _tr = {self._qe_track_expr(track)};"
            f"try {{ _tr.addTransition(_trans, false, 0, {start_timecode}, {duration_timecode}); }}"
            f"catch (_err) {{ _tr.addTransition(_trans, false, {start_timecode}, {duration_timecode}); }}"
        )
        try:
            self._eval(script, timeout=40.0)
        except Exception as exc:
            raise PremiereError(f"添加转场失败（{preset_name}）：{exc}") from exc

    def apply_effect(self, handle: ClipHandle, *, effect: str, params: dict) -> None:
        name_literal = self._escaped(effect)
        script = (
            f"var _effect = qe.project.getVideoEffectByName({name_literal});"
            f"if (!_effect) {{ throw new Error('Premiere 里没有名为 ' + {name_literal} + ' 的效果'); }}"
            f"var _tr = {self._qe_track_expr(handle.track)};"
            "var _target = null;"
            "for (var _i = 0; _i < _tr.numItems; _i++) {"
            "  var _item = _tr.getItemAt(_i);"
            f"  if (Math.abs(_item.start.seconds - {handle.start}) < 0.05) {{ _target = _item; break; }}"
            "}"
            f"if (_target === null) {{ throw new Error('QE 里找不到起点在 {handle.start:.2f}s 的片段'); }}"
            "_target.addVideoEffect(_effect);"
        )
        try:
            self._eval(script, timeout=40.0)
        except Exception as exc:
            raise PremiereError(f"添加效果失败（{effect}）：{exc}") from exc

        if not params:
            return
        applied = self._set_effect_params(handle, params)
        missing = [key for key in params if key not in applied]
        if missing:
            logger.warning(
                "效果 %s 上没找到这些参数（已按显示名匹配）：%s", effect, "、".join(missing)
            )

    def _set_effect_params(self, handle: ClipHandle, params: dict) -> set[str]:
        """按参数的显示名去效果组件上匹配并写值（Lumetri 之类参数名区分大小写）。"""

        applied: set[str] = set()
        clip = self._native_clip(handle)
        wanted = {str(key).lower(): value for key, value in params.items()}
        try:
            for component in clip.components:
                for prop in component.properties:
                    display = str(getattr(prop, "displayName", ""))
                    if display.lower() in wanted:
                        prop.setValue(wanted[display.lower()], True)
                        applied.add(display)
        except Exception as exc:  # noqa: BLE001 - 参数写失败不该让整条方案失败
            logger.warning("写入效果参数时出错（%s）：%s", handle.ref, exc)
        return applied

    def set_volume(self, handle: ClipHandle, *, db: float) -> None:
        clip = self._native_clip(handle)
        try:
            for component in clip.components:
                if "volume" not in str(getattr(component, "displayName", "")).lower():
                    continue
                for prop in component.properties:
                    if "level" in str(getattr(prop, "displayName", "")).lower():
                        prop.setValue(float(db), True)
                        return
        except Exception as exc:  # pragma: no cover
            raise PremiereError(f"设置音量失败（{handle.ref}）：{exc}") from exc
        raise PremiereError(f"片段 {handle.ref} 上找不到 Volume/Level 参数，无法设置音量")

    def add_marker(self, *, at: float, name: str, color: str, duration: float) -> None:
        sequence = self._sequence_or_fail()
        try:
            marker = sequence.markers.createMarker(float(at))
            marker.name = name
            marker.comments = name
            # 注意：Marker 的 start/end 要的是 float（秒），不是 Time 对象
            if duration:
                marker.end = float(at) + float(duration)
        except Exception as exc:  # pragma: no cover
            raise PremiereError(f"添加标记失败（{name}@{at}）：{exc}）") from exc

    # ---------------------------------------------------------------- 导出
    def export_sequence(self, *, output: Path, preset: str | None, format: str) -> list[Path]:
        sequence = self._sequence_or_fail()
        if not preset:
            raise PremiereError(
                "真实导出需要 Adobe Media Encoder 预设（.epr）的完整路径，请在 export_sequence "
                "操作里填 preset 字段（例如 C:\\Program Files\\Adobe\\Adobe Media Encoder 2026\\"
                "Presets\\H264_HD_1080p.epr）"
            )
        preset_path = Path(preset)
        if not preset_path.is_file():
            raise PremiereError(f"导出预设不存在：{preset_path}")

        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            encoder = self._project().encoder
            encoder.encodeSequence(
                sequence,
                str(output),
                str(preset_path),
                encoder.ENCODE_ENTIRE,
                False,
            )
        except Exception as exc:  # pragma: no cover
            raise PremiereError(f"提交 Media Encoder 导出失败：{exc}") from exc
        logger.info("已把导出任务交给 Adobe Media Encoder：%s", output)
        return [output] if output.exists() else []

    # ---------------------------------------------------------------- 观测
    def snapshot(self) -> TimelineState | None:
        try:
            return self._read_timeline()
        except PremiereUnavailableError:
            return None
        except Exception as exc:  # pragma: no cover
            logger.debug("读取时间线失败：%s", exc)
            return None

    def _read_timeline(self) -> TimelineState | None:
        sequence = self._sequence
        if sequence is None:
            return None
        settings = self._sequence_settings(sequence)
        state = TimelineState(
            sequence_name=str(getattr(sequence, "name", "AI Sequence")),
            width=settings["width"],
            height=settings["height"],
            fps=settings["fps"],
        )
        self._clip_registry.clear()
        self._handles.clear()
        for kind, prefix, collection in (
            ("video", "V", sequence.videoTracks),
            ("audio", "A", sequence.audioTracks),
        ):
            for index in range(int(collection.numTracks)):
                native_track = collection[index]
                track = TimelineTrack(name=f"{prefix}{index + 1}", kind=kind)
                for position, clip in enumerate(self._clips(native_track)):
                    name = str(getattr(clip, "name", f"clip{position}"))
                    clip_id = f"{track.name}:{position}:{name}"
                    self._clip_registry[clip_id] = clip
                    track.clips.append(
                        TimelineClip(
                            id=clip_id,
                            name=name,
                            media_ref=name,
                            track=track.name,
                            start=self._seconds(getattr(clip, "start", None)),
                            end=self._seconds(getattr(clip, "end", None)),
                            source_in=self._seconds(getattr(clip, "inPoint", None)),
                            source_out=self._seconds(getattr(clip, "outPoint", None)),
                        )
                    )
                state.tracks.append(track)

        try:
            for marker in self._collect_markers(sequence):
                state.markers.append(marker)
        except Exception as exc:  # pragma: no cover
            logger.debug("读取标记失败：%s", exc)
        return state

    def _collect_markers(self, sequence: Any) -> list[TimelineMarker]:
        """pymiere 的 MarkerCollection 没有实现遍历/索引，只能退化成取首尾标记。"""

        collection = getattr(sequence, "markers", None)
        if collection is None:
            return []
        try:
            return [self._marker_to_model(marker) for marker in collection]
        except TypeError:
            markers: dict[float, TimelineMarker] = {}
            for getter in ("getFirstMarker", "getLastMarker"):
                try:
                    marker = getattr(collection, getter)()
                except Exception:  # noqa: BLE001
                    continue
                if marker is None:
                    continue
                model = self._marker_to_model(marker)
                markers[model.at] = model
            if getattr(collection, "numMarkers", 0) > len(markers):
                logger.debug(
                    "序列里有 %s 个标记，但 pymiere 只能读到首尾各一个",
                    getattr(collection, "numMarkers", "?"),
                )
            return [markers[key] for key in sorted(markers)]

    def _marker_to_model(self, marker: Any) -> TimelineMarker:
        # pymiere 的 Marker 有点精神分裂：start/end 的 getter 返回 Time，setter 却要 float，
        # 一旦被写成 float 再读就会抛 TypeError，所以这里必须防御性地读。
        marker_start = self._safe_seconds(marker, "start")
        marker_end = self._safe_seconds(marker, "end", marker_start)
        return TimelineMarker(
            at=marker_start,
            name=str(getattr(marker, "name", "marker")),
            color="green",
            duration=max(0.0, marker_end - marker_start),
        )

    def _safe_seconds(self, obj: Any, attribute: str, default: float = 0.0) -> float:
        try:
            return self._seconds(getattr(obj, attribute, None), default)
        except Exception as exc:  # noqa: BLE001 - 属性读写不一致时退化成默认值
            logger.debug("读取 %s 失败，使用默认值 %s：%s", attribute, default, exc)
            return float(default)

    def _sequence_settings(self, sequence: Any) -> dict[str, float]:
        """读取序列规格。Sequence 的 videoFrameRate 是「一帧多少秒」，所以要取倒数。"""

        result = {"width": 1920.0, "height": 1080.0, "fps": 30.0}
        try:
            settings = sequence.getSettings()
            frame_rate = getattr(settings, "videoFrameRate", None)
            frame_seconds = getattr(frame_rate, "seconds", None)
            if frame_seconds:
                result["fps"] = round(1.0 / float(frame_seconds), 3)
            elif frame_rate:
                result["fps"] = float(frame_rate)
            width = getattr(settings, "videoFrameWidth", None)
            height = getattr(settings, "videoFrameHeight", None)
            if width:
                result["width"] = float(width)
            if height:
                result["height"] = float(height)
        except Exception as exc:  # pragma: no cover
            logger.debug("读取序列规格失败：%s", exc)
        return result

    # ---------------------------------------------------------------- QE 辅助
    @staticmethod
    def _qe_track_expr(track: str) -> str:
        """QE 轨道对象的 ExtendScript 表达式。

        QE 只能从 ExtendScript 里直接调：pymiere 把 QE 包成 PymiereGenericObject，靠 reflect
        查方法名，而真机上 QE 对象查不到 `getVideoTrackAt`，所以这里一律发原生脚本。
        """

        upper = track.upper()
        digits = upper[1:] if upper[1:].isdigit() else "1"
        index = max(0, int(digits) - 1)
        getter = "getVideoTrackAt" if upper.startswith("V") else "getAudioTrackAt"
        return f"qe.project.{getter}({index})"

    def _timecode(self, seconds: float) -> str:
        """把秒转成序列显示格式的时间码（QE 的 razor/addTransition 需要）。"""

        try:
            return str(self._wr().timecode_from_seconds(float(seconds), self._sequence_or_fail()))
        except Exception as exc:  # noqa: BLE001 - 兜底自己格式化
            logger.debug("用 pymiere 转时间码失败，改用本地格式化：%s", exc)
            fps = self._sequence_settings(self._sequence_or_fail())["fps"] or 30.0
            return self._format_timecode(seconds, fps)

    @staticmethod
    def _format_timecode(seconds: float, fps: float) -> str:
        fps_int = max(1, int(round(fps)))
        total_frames = int(round(max(0.0, seconds) * fps_int))
        frames = total_frames % fps_int
        total_seconds = total_frames // fps_int
        return (
            f"{total_seconds // 3600:02d}:"
            f"{(total_seconds // 60) % 60:02d}:"
            f"{total_seconds % 60:02d}:{frames:02d}"
        )

    @staticmethod
    def _all_track_names(sequence: Any) -> list[str]:
        names = [f"V{index + 1}" for index in range(int(sequence.videoTracks.numTracks))]
        names += [f"A{index + 1}" for index in range(int(sequence.audioTracks.numTracks))]
        return names

    # ---------------------------------------------------------------- 片段注册表
    def _register_native(self, ref: str, native_clip: Any, handle: ClipHandle) -> None:
        if ref:
            self._clip_registry[ref] = native_clip
            self._handles[ref] = handle
        self._handles.setdefault(handle.ref, handle)

    def _native_clip(self, handle: ClipHandle) -> Any:
        clip = self._clip_registry.get(handle.ref)
        if clip is None:
            # 可能是按「轨道 + 时间」解析出来的引用，刷新一次快照再找
            self._read_timeline()
            clip = self._clip_registry.get(handle.ref)
        if clip is None:
            raise PremiereError(f"Premiere 时间线上找不到片段：{handle.ref}")
        return clip
