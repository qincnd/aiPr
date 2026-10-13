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
| `createNewSequenceFromClips` 后时间线里凭空多了片段 | 这个接口会把传入的素材**真的插进序列** | 优先用预设建空序列；降级到它时先把自动插入的片段清掉 |
| 序列预设传空串会弹对话框 | `createNewSequence(name, presetPath)` 需要真实存在的 `.sqpreset` | 扫描 `Settings\SequencePresets` 按分辨率/帧率/横竖屏打分挑选 |

## 操作 → API 对照与成熟度

| 操作 | 实现方式 | 成熟度 |
| --- | --- | --- |
| `import_media` | 原生 `app.project.importFiles(...)`，再按 `mediaPath` 回查 ProjectItem | 原生 |
| `create_sequence` | `project.createNewSequence(name, 预设路径)`，预设自动挑选 | 原生 |
| `append_clip` / `insert_clip` | `track.insertClip(projectItem, Time)` + 按起点找回片段 + 写 in/out | 原生 |
| `move_clip` | `pymiere.wrappers.move_clip(clip, delta)` | 视版本（跨轨道做不到，会告警） |
| `trim_clip` | 写 `inPoint/outPoint/start/end`（Time） | 原生 |
| `add_marker` | `sequence.markers.createMarker(seconds)` + 写 `name/end` | 原生 |
| `set_volume` | 遍历 `clip.components` 找 Volume → Level 参数 `setValue(db)` | 视版本 |
| `razor` | QE `qe.project.getVideoTrackAt(i).razor(timecode)` | 视版本 |
| `delete_range` | 边界裁剪 + `TrackItem.remove()`，必要时借 QE 切两刀 | 视版本 |
| `apply_transition` | QE `getVideoTransitionByName()` + `track.addTransition(...)` | 视版本（签名随版本变化） |
| `apply_effect` | QE `getVideoEffectByName()` + `clip.addVideoEffect()`，参数按显示名匹配 | 视版本 |
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
