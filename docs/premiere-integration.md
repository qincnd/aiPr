# 接入真实的 Adobe Premiere Pro

框架默认用 **mock** 后端：它在内存里维护一条时间线，不启动 Premiere，用来跑通链路、做回归测试，
导出时额外产出可直接导入 Premiere 的 **CMX3600 EDL** 和时间线快照。

要真正操作 Premiere，把后端切成 `pymiere`。下面是完整的接入与验证流程。

## 链路

```
Python(pymiere)  --HTTP-->  CEP 面板(aiPr Link)  --evalScript-->  Premiere Pro 的 ExtendScript
   127.0.0.1:3000
```

面板由本仓库提供：[premiere-bridge/com.aipr.premiere.link](../premiere-bridge/com.aipr.premiere.link)，
它做三件事：

1. 在 `127.0.0.1:3000` 起本地 HTTP 服务：`GET /` 返回 `Premiere is alive`（探活），
   `POST / {"to_eval": "<jsx>"}` 在 Premiere 里执行并把返回值原样回传；
2. 加载 [jsx/runtime.jsx](../premiere-bridge/com.aipr.premiere.link/jsx/runtime.jsx)，把 pymiere 需要的
   ExtendScript 运行时（`$._pymiere` 对象注册表、`ExtendJSON`、`internal_variables_replacer`）注入 Premiere；
3. 在面板上显示监听状态、请求计数和最近执行过的脚本，便于排查。

> 为什么不用 pymiere 官方的 `Pymiere Link` 面板？官方只发了一个 `.zxp` 二进制包，本机网络策略拿不到。
> 好在 pymiere 的 Python 侧已经把协议和运行时依赖写得很清楚（见 `pymiere/core.py`），
> 自己实现一个等价面板反而更可控：可以针对 Premiere 2026 放宽版本范围，并顺手修掉 pymiere 的两个 bug（见下）。

## 安装

```powershell
# 1) Python 依赖（pymiere + setuptools；后者是必需的，见下）
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-premiere.txt

# 2) 安装 CEP 面板（复制到 %APPDATA%\Adobe\CEP\extensions 并打开 PlayerDebugMode）
powershell -ExecutionPolicy Bypass -File scripts\install-premiere-bridge.ps1 -Check

# 3) 重启 Premiere Pro，打开一个工程，然后【窗口 > 扩展 > aiPr Link】把面板打开
#    （面板打开后会留在工作区里，下次启动 Premiere 会自动恢复；
#      也可以在【窗口 > 工作区 > 全部面板】里一次性看到所有 CEP 面板）

# 4) 连通性自检
.\.venv\Scripts\python.exe scripts\check_premiere.py
```

`PlayerDebugMode` 是必需的：CEP 只加载已签名的扩展，未签名扩展必须打开调试模式
（脚本会写 `HKCU\Software\Adobe\CSXS.9` ~ `CSXS.14`，只动当前用户，不需要管理员权限）。

自检通过后，把后端切过去即可：

```powershell
$env:AIPR_PREMIERE_BACKEND = "pymiere"
cd backend; ..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

前端顶栏会显示 `pymiere 已就绪（已连接 Premiere Pro 26.0.0…）`。

## 没有 Premiere 也能验证

面板与适配器都可以脱离 Premiere 自测——[scripts/selftest_bridge.mjs](../scripts/selftest_bridge.mjs) 是一个
「假 CEP 宿主 + Premiere 对象模型仿真器」，里面加载的是**真实面板代码**与**真实 runtime.jsx**：

```powershell
.\.venv\Scripts\python.exe scripts\selftest_bridge.py   # 协议 / 运行时 / 时间对象 / 错误序列化
.\.venv\Scripts\python.exe scripts\selftest_adapter.py  # 适配器：导入→建序列→追加/插入/移动/修剪→标记→快照
```

仿真器会把 `app.path` 指向真实的 Premiere 安装目录，所以**序列预设解析**也是拿真机的
`Settings\SequencePresets` 验证的（例如 1080p25 → `HDV 1080p25.sqpreset`，
竖屏 1080x1920@30 → `Social Media Portrait 4x5 30 fps.sqpreset`）。

## 与 pymiere / Premiere 打交道时的坑（都已在本仓库绕开）

这些是实测出来的，改适配器时别踩回去：

| 现象 | 真相 | 本仓库的做法 |
| --- | --- | --- |
| `import distutils` 失败 | pymiere 仍在 `import distutils.version`，而 distutils 在 Python 3.12 被移除 | requirements 里带上 `setuptools`（提供兼容层） |
| 传字符串后 ExtendScript 报 `Unexpected string` | `pymiere.core._format_object_to_es` 把字符串格式化成 `"""text"""`，ExtendScript 不认 | 面板 `main.js` 把 `"""x"""` 还原成 `"x"`；适配器里路径自己用 JSON 转义 |
| `project.importFiles(..., None, ...)` 报类型错误 | pymiere 对 `targetBin` 做了严格类型检查，`None` 不合法；且 `Project` 上没有包 `createBin` | 导入改用原生 `eval_script`（顺带支持素材箱） |
| 把 `clip.start` 当数字用会报错 | `start/end/inPoint/outPoint` 是 **Time 对象**（读写都要 Time） | 统一走 `_time()` / `_seconds()` 两个辅助方法 |
| `track.insertClip()` 返回 None | pymiere 的 `Track.insertClip` 是无返回值包装，ExtendScript 侧本身也是 void | 插入后按起点时间在轨道上把片段找回来 |
| `Marker.end` 写入后再读会抛异常 | getter 返回 `Time`，setter 却要 `float`，写回后再读就崩 | 用防御性读取 `_safe_seconds()` |
| 遍历 `sequence.markers` 报错 | pymiere 的 `MarkerCollection` 没实现 `__iter__`/`__getitem__` | 退化成 `getFirstMarker()`/`getLastMarker()`，读不到时只记日志 |
| `createNewSequenceFromClips` 后时间线里凭空多了片段 | 这个接口会把传入的素材**真的插进序列** | 默认用它按参照素材建序列，随后清除自动插入的片段；方案显式提供 `preset` 时才走预设路径 |
| `createNewSequence(name, presetPath)` 会弹对话框或阻塞 | 必须传真实存在的 `.sqpreset`；传错误/空路径可能让对话框挡住后续 ExtendScript | 按分辨率/帧率/横竖屏扫描 `Settings\SequencePresets` 自动挑选，并给 `_eval` 加超时保护 |
| Premiere 界面被弹窗挡住后，后续所有脚本调用永久卡住 | `createNewSequence` 会弹「新建序列」对话框，而对话框阻塞 ExtendScript 引擎 | 默认改走 `createNewSequenceFromClips([一个素材])`（不弹窗、规格天然匹配素材）再清空；所有 `_eval` 都带超时，超时抛出「可能弹了对话框」的可操作错误 |
| QE 的 `addTransition` 加转场「成功」但时间线上没有转场 | QE 轨道的 item 是 **Clip/Empty 交替** 的（第 k 个片段在索引 2k），按序号定位会打到空 item 上；空 item 上调用是静默失效 | 不再用序号定位，改成在 JS 里**按片段名 + 出现次序**扫描 QE item |
| QE 的 `addTransition` 抛 `Illegal Parameter type` | 可用签名只有 `addTransition(transition, forceSingle, durationTimecode)` 三个参数，多传一个（哪怕是 `0`）就报错；`forceSingle=true` 会把转场拉满整段长度，不能用 | 只传三个参数，`forceSingle` 固定 `false`，时长用序列时间码 |
| 转场加到了错误的剪辑点 | `addTransition` 对齐的是**被选中片段的出点**，不是入点 | 在剪辑点 C 处加转场时，选「结束于 C 的那个片段」 |
| `getVideoEffectByName('Lumetri Color')` 不报错但加不上效果 | 名字不存在时它只返回一个 **`name` 为空的对象**（不抛异常），拿它去 `addVideoEffect` 是静默无操作 | 先按官方列表解析真实名字：`getVideoEffectList()`/`getVideoTransitionList()` + 别名表 + 归一化模糊匹配；调用后再回读类型化 API（组件数/转场数）确认真的生效，否则抛错 |
| 效果名 / 参数名和文档对不上 | 中文版 Premiere 的效果名是**本地化**的（`Lumetri 颜色`、`裁剪`、`黑白`），组件参数也只有本地化 `displayName`（`曝光`、`对比度`） | 效果名走别名表（`EFFECT_ALIASES`）+ 列表模糊匹配；参数匹配不上时在日志里列出该效果的可用参数名 |

## Premiere 2026（26.0）真机上实测到的行为

这些结论是在本机 PR 2026 / 26.0.0.72 + 中文界面下逐条验证出来的：

* **QE 只能从 ExtendScript 里原生调用**。pymiere 把 QE 包成 `PymiereGenericObject`，靠 `reflect` 查方法名，
  而 QE 对象上查不到这些方法；另外 `qe.project` 上**没有** `getVideoTrackAt`，轨道必须从
  `qe.project.getActiveSequence().getVideoTrackAt(i)` 取。
* **QE 轨道的 item 是 Clip/Empty 交替的**：`numItems` 是片段数的两倍，第 k 个片段在索引 `2k`，
  索引 `2k+1` 是占位空 item。所以「按序号定位片段」在这个版本上是不可靠的。
* **转场**：`item.addTransition(transition, false, "00:00:00:15")`（三参数）——
  转场落在**该片段的出点**上；`forceSingle=true` 会让转场长度变成整段片段时长。
* **效果**：`item.addVideoEffect(effect)` 是两参数（多传 `0` 就报 `Illegal Parameter type`）；
  加载后的组件名和参数名都是**界面语言**。
* **音量**：Volume/Level 组件在音频片段上；V 轨视频片段没有该组件。中文版名称是「音量 / 级别」。
  `set_volume` 应引用 A1 等音频轨上的音频片段。
* **切割**：轨道级 `track.razor(timecode)`，整条时间线 `sequence.razor(timecode)`；
  QE 片段的 `start/end` 是 `QETime`（读不出秒数），所以片段定位都在 Python 侧用类型化 API 算好。
* **转场/效果读回**：转场挂在 **`Track.transitions`** 上（`TrackItem.transitions` 是 `undefined`），
  与片段一起在快照里回读；片段效果从 `clip.components` 里回读并过滤掉
  Premiere 自带的 `运动`/`不透明度`/`Volume` 组件。
* **序列规格**：`createNewSequenceFromClips` 建出来的序列规格跟参照素材走，
  所以方案里写的 `1920x1080@30` 可能实际是 `1280x720@29.97`——执行器会在事件流里给出警告，
  要强制指定请给 `create_sequence` 填 `preset`。
* **导出**：`project.encoder.encodeSequence(...)` 的预设参数必须是真实存在的 `.epr` 文件，
  不填就只会在工程里加一条导出任务或直接失败；`export_sequence` 因此仍是「需要预设文件」。

## 操作 → API 对照与成熟度

| 操作 | 实现方式 | 成熟度 |
| --- | --- | --- |
| `import_media` | 原生 `app.project.importFiles(...)`，再按 `mediaPath` 回查 ProjectItem | 原生 |
| `create_sequence` | 默认用参照素材 `createNewSequenceFromClips` + 清空（不弹窗）；给了 `preset` 才走 `createNewSequence(name, 预设路径)` | 原生 |
| `append_clip` / `insert_clip` | `track.insertClip(projectItem, Time)` + 按起点找回片段 + 写 in/out | 原生 |
| `move_clip` | `pymiere.wrappers.move_clip(clip, delta)` | 视版本（跨轨道做不到，会告警） |
| `trim_clip` | 写 `inPoint/outPoint/start/end`（Time） | 原生 |
| `add_marker` | `sequence.markers.createMarker(seconds)` + 写 `name/end` | 原生 |
| `set_volume` | 在音频片段上找「音量 / 级别」组件与参数并调用 `setValue(db)`；要放在 A 轨音频片段上 | PR 2026 已验证 |
| `razor` | QE `sequence.razor(timecode)` / `track.razor(timecode)` | 原生（PR 2026 已验证） |
| `delete_range` | 边界裁剪 + `TrackItem.remove()`，必要时借 QE 切两刀 | 视版本 |
| `apply_transition` | QE `getVideoTransitionByName()` + `item.addTransition(tr, false, timecode)`（对齐片段出点） | 原生（PR 2026 已验证） |
| `apply_effect` | QE `getVideoEffectByName()` + `item.addVideoEffect()`，名字先按官方列表解析成本地化名称 | 原生（PR 2026 已验证） |
| `export_sequence` | `project.encoder.encodeSequence(...)`，**必须**填 `.epr` 预设路径 | 需要预设文件 |

「视版本」的几条走 QE（Adobe 未公开文档、签名随版本变化），失败时会抛出带原因的 `PremiereError`
并继续执行后续操作。`GET /api/system` 的 `premiere.capabilities` 会返回每个操作的成熟度
（native / partial / simulated），前端顶栏据此显示可用性。
## 其他接入方式

* **自研 CEP 面板**：就是本仓库的做法（见上文）；换协议只改 `js/main.js` 与 `PremiereAdapter` 实现。
* **UXP 插件（Premiere 25+ 的新扩展体系）**：UXP 跑在 Node/JS 侧，Python 无法直接调用，
  需要自己再搭一层 RPC；适配器接口不变，替换实现即可。
* **生成工程文件人工导入**：不做实时控制，把 Edit Plan 编译成 EDL（Mock 后端已支持）、
  FCPXML/AAF，或往模板 `.prproj` 里注入素材引用。跨版本最稳，但读不到工程当前状态。
