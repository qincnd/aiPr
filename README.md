# aiPr · 用大模型驱动 Adobe Premiere Pro 做剪辑

在前端网页里填**两样东西**——一段自然语言的剪辑提示词 + 一个素材目录——后端就会：

1. 扫描素材目录（ffprobe 读取时长/分辨率，没装 ffmpeg 也能降级运行）；
2. 让大模型把提示词翻译成一份结构化的**剪辑方案（Edit Plan）**；
3. 把方案翻译成对 **Premiere Pro** 的一连串操作，实时把日志推给前端。

> 当前是**框架版**：分层、接口、DSL、Mock 后端、前后端、测试、文档都已就位；
> 真实控制链路（pymiere + 自研 CEP 面板）**已在 Premiere Pro 2026（26.0.0.72，中文界面）真机上逐条验证**：
> 建序列/追加/插入/移动/修剪/切割/转场/效果/标记/快照全部实测可用
> （完整步骤与 PR 2026 的 QE 实测结论见 [premiere-integration.md](docs/premiere-integration.md)）。

## 已经能跑

* 前端：素材目录输入 + 扫描 + 勾选素材、提示词与参数输入、方案预览（可手改 JSON）、
  执行日志（WebSocket 实时流 + 断线自动轮询兜底）；
* 后端：素材扫描/白名单校验、方案生成（大模型 + 启发式兜底）、任务队列（串行、可取消）、
  产物落盘、`/docs` 自动 API 文档；
* 剪辑 DSL：13 种操作（建序列、追加/插入/移动/修剪片段、切割、删区间、转场、效果、音量、
  标记、导出），5 个测试模块共 **51 个用例全部通过**；
* Mock 后端：内存时间线，导出时额外产出**可直接导入 Premiere 的 EDL** 与时间线快照 JSON；
* 真实后端：`pymiere` + 自研 CEP 面板（[premiere-bridge/](premiere-bridge/)）驱动本机 Premiere Pro，
  另有 **2 个脱离 Premiere 的自测脚本**（协议自测 + 适配器自测，带 Premiere 对象模型仿真器）。

真机实测（30 条操作的完整任务，`AI Sequence`：15 个片段 + 14 个转场 + 效果）：**30/30 条操作成功**，
转场逐个落在剪辑点上、效果真的挂到片段组件上，时间线快照可完整回读。

## 目录结构

```
aiPr/
├── backend/                 Python · FastAPI
│   ├── app/
│   │   ├── main.py          应用入口（含前端静态托管）
│   │   ├── api/routes/      health / system / media / plan / jobs(+WebSocket)
│   │   ├── core/            配置、异常、日志
│   │   ├── models/          素材库、剪辑方案 DSL、任务、时间线
│   │   └── services/
│   │       ├── media_scanner.py   素材扫描（ffprobe，可选）
│   │       ├── planner/           大模型方案生成 + 启发式兜底
│   │       ├── executor.py        方案执行器（唯一懂方案语义的地方）
│   │       ├── jobs/manager.py    任务队列、事件流、持久化
│   │       └── premiere/          适配器层：mock / pymiere / factory
│   ├── tests/               47 个测试
│   ├── requirements*.txt
│   └── .env.example
├── frontend/                Vue 3 + Vite + TypeScript
│   └── src/{api,components,composables}
├── premiere-bridge/         驱动 Premiere 的 CEP 面板（aiPr Link）
│   └── com.aipr.premiere.link/{CSXS/manifest.xml, index.html, js/, jsx/}
├── scripts/                 安装 / 自检 / 仿真器
│   ├── install-premiere-bridge.ps1   安装 CEP 面板 + 打开 PlayerDebugMode
│   ├── check_premiere.py             连通性自检（pymiere / 面板 / ExtendScript）
│   ├── selftest_bridge.{mjs,py}      面板协议自测（含假 CEP 宿主）
│   └── selftest_adapter.py           适配器自测（含 Premiere 对象模型仿真器）
├── docs/
│   ├── architecture.md          分层与请求链路
│   ├── edit-plan-schema.md      剪辑方案 DSL 参考
│   └── premiere-integration.md  真实接入 Premiere 的完整流程与踩坑记录
├── media/                   默认素材目录（内容不入库）
└── workspace/               运行产物：runs/<job_id>/、exports/、mock/
```

## 快速开始

前置：Python 3.11+（本仓库在 3.14 上开发验证）、Node 20+。

```powershell
# 1) 后端依赖
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt

# 2) 前端依赖 + 构建（构建产物会被后端直接托管）
cd frontend; npm install; npm run build; cd ..

# 3) 配置（可选：不配 API Key 也能跑，会自动用启发式排片）
Copy-Item backend\.env.example backend\.env

# 4) 启动
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

打开 <http://127.0.0.1:8000> 就是操作台；<http://127.0.0.1:8000/docs> 是 API 文档。

前端热更新开发模式（改代码即时生效）：

```powershell
cd frontend; npm run dev     # http://localhost:5173，/api 已代理到 8000
```

### 最小可用流程

1. 左侧「素材目录」填一个目录（必须落在 `AIPR_MEDIA_ROOTS` 白名单内，默认包含项目下的 `media/`），
   点**扫描素材**；
2. 中间填提示词，例如「把素材剪成 30 秒的快节奏短片，镜头之间加叠化，配一段背景音乐，最后导出成片」，
   点**生成方案**（大模型会返回 Edit Plan，可切到「编辑 JSON」手工改）；
3. 点**在 Premiere 中执行**（默认 mock 后端），右侧实时打印每一步操作、进度、时间线快照与产物路径。

## 接入真实的 Premiere Pro

本机已安装 Premiere Pro 时，把后端切成 `pymiere` 即可真实驱动它（完整步骤与踩坑记录见
[docs/premiere-integration.md](docs/premiere-integration.md)）：

```powershell
#配置虚拟环境
python -m venv .venv
.venv\Scripts\activate
# 1) 装 Python 依赖（含 pymiere 与 setuptools）
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-premiere.txt

# 2) 安装 CEP 面板（复制到 %APPDATA%\Adobe\CEP\extensions + 打开 PlayerDebugMode）
powershell -ExecutionPolicy Bypass -File scripts\install-premiere-bridge.ps1 -Check

# 3) 重启 Premiere，打开一个工程，然后【窗口 > 扩展 > aiPr Link】

# 4) 自检连通性
.\.venv\Scripts\python.exe scripts\check_premiere.py

# 5) 切换后端
$env:AIPR_PREMIERE_BACKEND = "pymiere"
cd backend; ..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

没装 Premiere 也能验证整条真实链路——仓库自带假 CEP 宿主与 Premiere 对象模型仿真器：

```powershell
.\.venv\Scripts\python.exe scripts\selftest_bridge.py   # 面板协议 + ExtendScript 运行时
.\.venv\Scripts\python.exe scripts\selftest_adapter.py  # 适配器全流程（导入→建序列→剪辑→快照）
```

## 配置

全部通过环境变量或 `backend/.env` 覆盖，前缀 `AIPR_`：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AIPR_HOST` / `AIPR_PORT` | 127.0.0.1 / 8000 | 监听地址 |
| `AIPR_MEDIA_ROOTS` | `media/`、Videos、Desktop、Downloads | 允许扫描的素材根目录（`;` 分隔），**前端无法越权访问** |
| `AIPR_WORKSPACE_DIR` | `workspace/` | 产物目录（`runs/`、`exports/`、`mock/`） |
| `AIPR_MAX_MEDIA_FILES` | 2000 | 单次扫描上限 |
| `AIPR_FFPROBE_PATH` | 自动查找 | ffprobe 路径，留空则从 PATH 找 |
| `AIPR_PREMIERE_BACKEND` | `mock` | `mock`（无需装 PR）/ `pymiere`（真实控制） |
| `AIPR_PREMIERE_PROJECT_PATH` | 空 | 真实后端的工程文件路径 |
| `AIPR_LLM_ENABLED` | true | 关掉就只用启发式排片 |
| `AIPR_LLM_BASE_URL` | `https://api.deepseek.com/v1` | 任何 OpenAI 兼容接口（OpenAI / 通义 / Kimi / Ollama / one-api…） |
| `AIPR_LLM_API_KEY` | 空 | 留空则自动降级到启发式 |
| `AIPR_LLM_MODEL` | `deepseek-chat` | 模型名 |
| `AIPR_LLM_TIMEOUT_SECONDS` | 120 | 单次请求超时 |
| `AIPR_LLM_TEMPERATURE` | 0.2 | 采样温度 |

## API 速查

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/system` | 后端能力矩阵、大模型/ffprobe 状态、素材白名单 |
| GET | `/api/media/roots` | 允许扫描的根目录 |
| POST | `/api/media/scan` | 扫描素材目录，返回素材库 |
| POST | `/api/plan` | 提示词 → 剪辑方案（不碰 Premiere，供人工确认） |
| POST | `/api/jobs` | 提交任务：给 `prompt` 或直接给 `plan`；返回 `job.id` |
| GET | `/api/jobs` / `/api/jobs/{id}` | 任务列表 / 详情（含时间线、产物） |
| GET | `/api/jobs/{id}/events` | 事件日志 |
| WS | `/api/ws/jobs/{id}` | 实时日志流（`snapshot` → `event`* → `done`） |
| POST | `/api/jobs/{id}/cancel` | 取消任务 |

## 测试

```powershell
# 单元测试（51 个用例）
cd backend; ..\.venv\Scripts\python.exe -m pytest; cd ..

# 真实链路自测（不需要装 Premiere）
.\.venv\Scripts\python.exe scripts\selftest_bridge.py
.\.venv\Scripts\python.exe scripts\selftest_adapter.py

# 前端类型检查
cd frontend; npm run typecheck; cd ..
```

## 路线图

已预留接口、尚未实现的能力：

* **文字/字幕**：需要 MOGRT 模板或旧版 Title，计划加 `add_text` 操作（模板路径 + 文本参数）；
* **素材内语义检索**：接入视觉/语音模型，按「找有人脸的特写」这类描述挑片段，用于替换现在的顺序排片；
* **自动卡点**：用 librosa 分析 BGM 鼓点，把 `razor`/`append_clip` 对齐到节拍；
* **变速与关键帧**：`set_speed`、关键帧曲线操作；
* **FCPXML/AAF 导出**：让「无桥接」路线也能带转场效果；
* **任务队列持久化**：目前任务在内存中，重启即丢（历史产物仍在 `workspace/runs/`）。

## 许可

仅用于个人学习与自有素材的剪辑自动化；请遵守 Adobe 相关许可协议。
