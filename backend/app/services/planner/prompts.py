"""给大模型看的系统提示词与用户提示词构造。"""

from __future__ import annotations

import json
from typing import Any

from app.models.media import MediaKind, MediaLibrary
from app.models.plan import plan_json_schema

OPERATION_REFERENCE = """\
可用操作（operations 数组元素，严格按数组顺序执行）：

| op | 参数 | 说明 |
| --- | --- | --- |
| create_sequence | name, width, height, fps, preset? | 新建序列。除 import_media 外必须是第一条 |
| import_media | media[], bin? | 显式导入素材到工程面板；append/insert 会自动导入，一般可省略 |
| append_clip | media, ref?, track, in_point, out_point? | 在轨道末尾追加片段（最常用） |
| insert_clip | media, ref?, track, at, in_point, out_point? | 在时间线 at 处插入，后续片段右移 |
| move_clip | clip, to_time, track? | 移动已有片段 |
| trim_clip | clip, in_point, out_point? | 把片段修剪到新的源入出点 |
| razor | at, track? | 在 at 时刻切一刀（track 省略表示全部轨道） |
| delete_range | start, end, track?, ripple | 删除时间区间，ripple=true 时吸附后续素材 |
| apply_transition | at, track, transition, duration | 相邻片段交界处加转场：cross_dissolve / dip_to_black / wipe |
| apply_effect | clip, effect, params | 加效果，例：effect="Lumetri Color", params={"Exposure": 0.5} |
| set_volume | clip, db | 调整片段音量，dB，负数降音量 |
| add_marker | at, name, color, duration? | 加标记，color 取 green/red/blue/yellow |
| export_sequence | output, preset?, format | 导出成片，必须是最后一条 |

时间单位一律为「秒」。media 字段填素材的 id 或文件名。
in_point / out_point 是「源素材内部」的时间；at / to_time / start / end 是「时间线」上的时间。

clip 的引用方式（三选一）：
- {"ref": "intro"} —— append_clip 时用 ref 命名的片段
- {"track": "V1", "at": 5.0} —— 时间线 5.0s 处该轨道上的片段
- {"track": "V1", "index": 0} —— 该轨道上第 0 个片段

轨道命名：视频 V1/V2/V3，音频 A1/A2。
"""

SYSTEM_PROMPT = f"""\
你是一位资深的视频剪辑师，同时精通 Adobe Premiere Pro 的自动化脚本。
你的任务：把用户的自然语言需求，翻译成一份结构化的剪辑方案（Edit Plan）JSON。

{OPERATION_REFERENCE}

硬性规则：
1. 只能使用「可用素材」列表里出现的素材，禁止编造素材名或路径。
2. 所有时间精确到 0.01 秒，且必须落在素材真实时长范围内。
3. 第一个操作必须是 create_sequence（import_media 可以排在最前面）。
4. 若用户要求导出/输出成片，最后一条操作必须是 export_sequence。
5. 成片总时长要贴近用户要求；相邻片段之间可以用 apply_transition 做过渡。
6. 宁可少写操作，也不要编造表中不存在的 op 或参数名。
7. 每条操作都可以带 note 字段，用中文一句话说明剪辑意图。

只输出一个 JSON 对象，不要输出 markdown 代码块、解释或多余文字。顶层结构：
{{
  "version": "1.0",
  "project_name": "工程名（英文/数字/下划线）",
  "summary": "中文剪辑思路，3 句以内",
  "sequence": {{"name": "序列名", "width": 1920, "height": 1080, "fps": 30.0, "preset": null}},
  "operations": [ ... ]
}}
"""


def build_system_prompt() -> str:
    return SYSTEM_PROMPT


def build_retry_prompt(error_message: str) -> str:
    """校验失败时，把 schema 和错误信息回灌给模型让它自我修复。"""

    schema = json.dumps(plan_json_schema(), ensure_ascii=False, separators=(",", ":"))
    return (
        "你上一次的输出无法通过校验，错误信息如下：\n"
        f"{error_message}\n\n"
        "请严格按下面的 JSON Schema 重新输出完整方案（只输出 JSON 对象，不要任何解释）：\n"
        f"{schema}"
    )


def _select_assets(library: MediaLibrary, media_ids: list[str]) -> list[Any]:
    if not media_ids:
        return library.assets
    wanted = {item.strip().lower() for item in media_ids if item.strip()}
    index = library.build_index()
    selected: list[Any] = []
    seen: set[str] = set()
    for key in wanted:
        asset = index.get(key) or library.find(key)
        if asset and asset.id not in seen:
            seen.add(asset.id)
            selected.append(asset)
    return selected


def build_user_prompt(
    *,
    prompt: str,
    library: MediaLibrary,
    media_ids: list[str],
    width: int,
    height: int,
    fps: float,
    target_duration: float | None,
    max_clips: int,
    extra_instructions: str = "",
    preset: str | None = None,
) -> str:
    assets = _select_assets(library, media_ids)
    video_count = sum(1 for item in assets if item.kind is MediaKind.VIDEO)
    audio_count = sum(1 for item in assets if item.kind is MediaKind.AUDIO)

    lines: list[str] = ["# 用户需求", prompt.strip(), "", "# 目标参数"]
    lines.append(f"- 序列：{width}x{height} @ {fps:g}fps")
    lines.append(f"- 目标成片时长：{target_duration} 秒" if target_duration else "- 目标成片时长：由你根据素材和需求判断")
    lines.append(f"- 最多使用 {max_clips} 个片段")
    if preset:
        lines.append(f"- 序列预设：{preset}")
    if extra_instructions.strip():
        lines.append(f"- 额外要求：{extra_instructions.strip()}")

    lines += [
        "",
        "# 可用素材（只能用这些，media 字段填 id 或文件名）",
        f"素材目录：{library.root}",
        f"共 {len(assets)} 个文件（视频 {video_count} 个、音频 {audio_count} 个）",
    ]
    for asset in assets:
        lines.append(f"- {asset.descriptor()}")

    lines += ["", "# 输出", "直接输出符合规范的 JSON 对象。"]
    return "\n".join(lines)
