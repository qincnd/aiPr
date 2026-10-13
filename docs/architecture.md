# 架构说明

## 一句话

**前端只负责收「提示词 + 素材目录」，后端把提示词变成一份中立的剪辑方案（Edit Plan），再由
执行器把方案落到可替换的「Premiere 后端」上。**

## 分层

```
┌────────────────────────────────────────────────────────────────────┐
│ 前端 Vue3 + Vite + TS (frontend/ 或后端托管的 frontend/dist)         │
│ ① 素材面板  ② 提示词面板  ③ 方案预览/编辑  ④ 执行日志(WebSocket)     │
└───────────────┬────────────────────────────────────────────────────┘
                │ REST + WebSocket  (/api/*)
┌───────────────▼────────────────────────────────────────────────────┐
│ API 层  backend/app/api/routes/*                                     │
│  /api/media/scan   /api/plan   /api/jobs   /api/ws/jobs/{id}  ...     │
└───────────────┬────────────────────────────────────────────────────┘
                │
┌───────────────▼────────────────────────────────────────────────────┐
│ 服务层  backend/app/services/                                         │
│                                                                       │
│  media_scanner   扫描素材目录（ffprobe 探测时长/分辨率，缺失则降级）   │
│  planner         PlannerService：LLMPlanner → 失败自动回退 Heuristic  │
│  executor        PlanExecutor：Edit Plan → 一连串适配器调用 + 事件     │
│  jobs.manager    JobManager：串行执行、事件流、持久化到 workspace/runs │
└───────────────┬────────────────────────────────────────────────────┘
                │  PremiereAdapter（统一契约）
┌───────────────▼────────────────────────────────────────────────────┐
│ 适配器层  backend/app/services/premiere/                              │
│   base.py          接口定义 + 能力矩阵                                 │
│   mock.py          内置内存时间线（无需装 PR，导出 EDL/时间线快照）     │
│   pymiere_adapter  真实驱动本机 Premiere Pro（pymiere / CEP）          │
│   factory.py       按 AIPR_PREMIERE_BACKEND 选择实现                   │
└────────────────────────────────────────────────────────────────────┘
```

## 一次请求的完整链路

1. **扫描素材**：`POST /api/media/scan` → `media_scanner.get_library()`（30s 缓存，路径白名单校验，
   ffprobe 并发探测）→ 返回素材库，前端展示并勾选。
2. **生成方案**：`POST /api/plan` → `PlannerService.create_plan()`
   * 有 API Key：`LLMPlanner` 把「提示词 + 素材清单（id/时长/分辨率）+ 操作表」交给 OpenAI 兼容接口，
     要求返回 JSON；pydantic 校验，失败则带错误信息重试一次；
   * 无 Key 或调用失败：`HeuristicPlanner` 按目标时长均匀排片 + 叠化 + BGM + 导出，保证链路可跑。
3. **执行方案**：`POST /api/jobs` → `JobManager` 建任务（不阻塞请求）→ 后台任务：
   * 加全局锁（同一时间只有一个任务操作 Premiere）；
   * `PlanExecutor` 逐条执行：解析素材名 → 解析片段引用（ref/轨道+时间/序号）→ 调用适配器；
   * 每条操作失败默认记录后继续（`continue_on_error`），最后统一快照时间线、保存工程、汇总产物。
4. **实时反馈**：前端订阅 `/api/ws/jobs/{id}`，收到 `snapshot`（任务元信息）、`event`（逐条日志）、
   `done`（结束）。WebSocket 断开时前端自动退回 HTTP 轮询。
5. **可复盘**：产物落在 `workspace/runs/<job_id>/`：`job.json`、`plan.json`、`timeline.json`、
   `events.jsonl`，导出文件在 `workspace/exports/`。

## 为什么中间要有一份「剪辑方案」

* **可复核**：人和大模型之间多一道「可读的 JSON」，出错时一眼能看出是哪一步的选择有问题；
* **可替换**：换大模型只改 planner，换 Premiere 接入方式只改适配器，执行器不用动；
* **可测试**：Mock 适配器让整套流程在没有 Premiere 的机器上也能跑测试（当前 47 个用例）。

## 扩展点

| 想做的事 | 改哪里 |
| --- | --- |
| 新增一种剪辑操作（如缩放、变速） | `models/plan.py` 加 Op → `services/executor.py` 加分支 → 各适配器实现 |
| 接入新的大模型服务 | 只要兼容 `POST {base_url}/chat/completions`，改 `.env` 即可；否则实现 `LLMClient` 的替身 |
| 接入新的 Premiere 控制方式（自研 CEP / UXP / 生成 prproj） | 实现 `PremiereAdapter` 并在 `premiere/factory.py` 注册 |
| 换前端框架 | 后端只依赖 `/api/*` 契约，前端可整体替换 |

## 并发与隔离

* 任务串行执行（`JobManager._lock`），因为真实场景下一个 Premiere 实例只能被一个脚本控制；
* 适配器调用统一丢到线程池（`asyncio.to_thread`），因为 pymiere 是同步阻塞调用；
* 素材路径与导出路径都做白名单校验（`AIPR_MEDIA_ROOTS` / `workspace/exports`），避免被前端传参读写任意文件。
