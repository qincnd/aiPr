# 剪辑方案 DSL（Edit Plan）

`Edit Plan` 是整个框架的**中间语言**：大模型只负责产出这份 JSON，执行器负责把它翻译成对
Premiere Pro 的调用。它也是前端「方案预览 / 手工改 JSON」的数据结构。

定义在 [plan.py](../backend/app/models/plan.py)，执行逻辑在 [executor.py](../backend/app/services/executor.py)。

## 顶层结构

```json
{
  "version": "1.0",
  "project_name": "ai_edit_project",
  "summary": "使用 4 个镜头，镜头之间以叠化过渡。",
  "sequence": { "name": "AI Sequence", "width": 1920, "height": 1080, "fps": 30.0, "preset": null },
  "operations": [ ... ]
}
```

| 字段 | 说明 |
| --- | --- |
| `project_name` | 工程名，也用作默认导出文件名 |
| `summary` | 中文剪辑思路，前端会展示，便于人工复核 |
| `sequence` | 序列规格；`preset` 填 Premiere 序列预设的完整路径时优先于宽高帧率 |
| `operations` | 按顺序执行的操作列表，至少一条 |
| `warnings` | 可选，生成器/校验器补充的提醒 |
| `source` | `llm` / `heuristic` / `manual` |

## 操作一览

时间单位一律是**秒**。`media` 填素材的 `id`、文件名或（允许目录内的）绝对路径。

| op | 参数 | 说明 |
| --- | --- | --- |
| `create_sequence` | `name, width, height, fps, preset?` | 新建序列，除 `import_media` 外必须是第一条 |
| `import_media` | `media[], bin?` | 显式导入素材；`media` 为空表示导入方案引用到的全部素材 |
| `append_clip` | `media, ref?, track, in_point, out_point?` | 在轨道末尾追加片段（最常用） |
| `insert_clip` | `media, ref?, track, at, in_point, out_point?` | 在 `at` 处插入，插入点之后的片段右移（落在片段内部会先切开） |
| `move_clip` | `clip, to_time, track?` | 移动已有片段 |
| `trim_clip` | `clip, in_point, out_point?` | 把片段修剪到新的**源**入出点 |
| `razor` | `at, track?` | 在 `at` 处切一刀；`track` 省略表示所有轨道 |
| `delete_range` | `start, end, track?, ripple` | 删除时间区间；`ripple=true` 时后续素材吸附上来 |
| `apply_transition` | `at, track, transition, duration` | 相邻片段交界处加转场：`cross_dissolve` / `dip_to_black` / `wipe` … |
| `apply_effect` | `clip, effect, params` | 加效果，例如 `effect="Lumetri Color", params={"Exposure": 0.5}` |
| `set_volume` | `clip, db` | 调整片段音量（dB，负数为降音量） |
| `add_marker` | `at, name, color, duration?` | 加标记，`color` 取 green/red/blue/yellow |
| `export_sequence` | `output, preset?, format` | 导出成片，必须是最后一条；`output` 相对 `workspace/exports` |

每条操作都支持两个通用字段：

* `note`：一句中文说明，前端展示、日志里也会带出来；
* `enabled`：置为 `false` 可跳过（预演/调试时很有用）。

## 片段引用（`clip` 字段）

后续操作要操作「已有片段」时，用下面三种方式之一定位：

```json
{ "ref": "intro" }                // append_clip 时用 ref 命名过
{ "track": "V1", "at": 5.0 }      // 时间线 5.0s 处该轨道上的片段
{ "track": "V1", "index": 0 }     // 该轨道上第 0 个片段
```

解析优先级是 `ref` > `track+at` > `track+index`。用 `ref` 最稳定，建议让大模型给关键片段命名。

## 轨道命名

视频 `V1`/`V2`/…，音频 `A1`/`A2`/…，与 Premiere 中一致。Mock 后端默认建好 `V1/V2/A1/A2` 四条轨道，
用到不存在的轨道会自动补一条。

## 最小可用方案

```json
{
  "project_name": "demo",
  "operations": [
    { "op": "create_sequence", "name": "Demo", "width": 1920, "height": 1080, "fps": 30 },
    { "op": "append_clip", "media": "shot_1.mp4", "ref": "a", "track": "V1", "in_point": 0, "out_point": 3 },
    { "op": "append_clip", "media": "shot_2.mp4", "ref": "b", "track": "V1", "in_point": 0, "out_point": 3 },
    { "op": "apply_transition", "at": 3, "track": "V1", "transition": "cross_dissolve", "duration": 0.5 },
    { "op": "export_sequence", "output": "demo.mp4", "format": "H.264" }
  ]
}
```

可以直接 `POST /api/jobs` 提交它（见 [README](../README.md)），也可以在 `/docs` 页面里 Try it out。

## 校验规则

* `operations` 不能为空；未知 `op` 或多余参数会被 pydantic 直接拒绝（`extra="forbid"`）；
* 生成器会检查引用的素材是否存在于素材库，不存在时给出 warning（执行时该条操作会失败，但默认继续执行后续操作）；
* 导出路径必须落在 `workspace/exports` 之内；
* 大模型输出校验失败时，会把 pydantic 的错误信息 + JSON Schema 回灌给模型重试一次，仍失败则回退到启发式方案。
