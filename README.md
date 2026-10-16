# agent_market_research

多智能体市场调研与营销内容生成系统。一次 HTTP 提交，异步跑完整条链路，产出
**调研报告 → 营销战略 → 活动创意 → 广告文案**。

> 本项目是 `testcrewaiflowsfullstack` 的**规范重建版**：同样的业务能力，
> 按 SDD（规格驱动）+ TDD（测试驱动）重做，并修掉源项目自认的已知缺陷
> （尤其是那个「失败作业被误报成功」的严重状态 bug）。
> 与源项目的完整差异清单见 [`spec/001-requirements.md`](spec/001-requirements.md) §6。

---

## 架构一览

```
调用方 ──POST /api/crew──► FastAPI (:8012) ──► Redis（Celery broker）
   ▲                          │ 立即返回 job_id      │
   │                          │                      ▼
   │                          │                Celery Worker
   │                          │                      │
   │                          │              CrewAI Flow 编排
   │                          │        ┌─────────────┴──────────────┐
   │                          │        │ @start  市场调研 Crew        │
   │                          │        │   └─ 首席市场分析师（检索+抓取）│
   │                          │        │            ↓ 上游产出即下游输入  │
   │                          │        │ @listen 内容创作 Crew        │
   │                          │        │   ├─ 首席营销战略师 → Strategy│
   │                          │        │   ├─           → CampaignIdea│
   │                          │        │   └─ 首席创意内容创作师 → Copy│
   │                          │        └────────────────────────────┘
   └──GET /api/crew/{id}──────┴──── MySQL（jobs / events）
```

代码分五层，依赖只能向内（`spec/010-architecture.md`）：

```
api / worker  →  services  →  ports  ←  adapters
                    ↓
                 domain
```

`services/` 只认 `ports/` 的 Protocol，不认识 MySQL / Celery / CrewAI。
因此单元测试注入内存替身即可离线全绿，换存储也不动业务代码。

---

## 快速开始

### 1. 前置条件

| 依赖 | 版本 | 用途 |
|---|---|---|
| Python | 3.12 – 3.13 | 后端 |
| [uv](https://docs.astral.sh/uv/) | ≥ 0.6 | 依赖管理 |
| MySQL | 8.x | 作业与事件存储 |
| Redis | 7.x | Celery broker |
| Node.js | ≥ 20 | 前端控制台（可选） |

```bash
# 基础设施（按发行版择一）
sudo service mysql start && sudo service redis-server start
```

### 2. 配置

```bash
cp .env.example .env
$EDITOR .env        # 至少填 OPENAI_API_BASE / OPENAI_API_KEY / OPENAI_MODEL_NAME 与 MySQL 口令
```

`.env` 已被 `.gitignore` 忽略；配置只从环境变量读，源码里没有任何密钥（`REQ-009`）。

### 3. 安装依赖

```bash
make install        # uv sync --extra dev
```

### 4. 启动

```bash
scripts/dev_up.sh           # 拉起 Worker + API，日志写到 .artifacts/
scripts/dev_up.sh status    # 看状态
scripts/dev_up.sh stop      # 停止
```

手工启动（想自己看前台日志时）：

```bash
make run-worker     # 终端 A
make run-api        # 终端 B
```

> 与源项目不同，**不再要求「必须在源码目录下启动」**：配置用绝对路径定位，
> 从任意工作目录启动都能读到（`NFR-006` / `TC-S-003`）。

### 5. 验证

```bash
curl -s http://127.0.0.1:8012/healthz
curl -s -X POST http://127.0.0.1:8012/api/crew \
  -H 'Content-Type: application/json' \
  -d '{"customer_domain":"openai.com","project_description":"为一家 AI 基础设施公司制定新品发布营销方案"}'
# {"job_id":"..."}

curl -s http://127.0.0.1:8012/api/crew/<job_id> | python3 -m json.tool
```

或者一把梭：

```bash
scripts/smoke.sh    # 提交 → 轮询到终态 → 校验结果形状
```

接口详情见 [`spec/022-contracts-http.md`](spec/022-contracts-http.md)，服务起来后也有 `/docs`。

### 6. 前端控制台（可选）

```bash
cd src/web
npm install
npm run dev         # http://localhost:5173，/api 由 Vite 代理到 :8012
```

---

## 开发

### 门禁（提交前必须全绿）

```bash
make lint      # ruff check + format --check
make type      # mypy 严格模式
make test      # 单元 + 契约 + 集成（**完全离线**，不需要 MySQL/Redis/密钥）
```

`make test` 只跑前三层；系统测试需要真实中间件：

```bash
make test-system                    # 真实 MySQL + Redis + Worker（用 fake 供应商，确定性）
make test-system -- --run-real-llm  # 额外跑真实模型网关（花钱、慢，默认关闭）
```

### 目录

```
spec/       规格——唯一事实来源，改行为先改这里
docs/       交付文档（技术说明 / 运行手册 / 测试计划）
src/amr/    后端：domain · ports · adapters · services · api · worker · crews
src/web/    前端：Vue 3 + TS + Vite
tests/      unit · contract · integration · system
scripts/    启动与冒烟脚本
```

### 协作流程

本项目由多 Agent 流水线交付，规矩写在 [`spec/000-constitution.md`](spec/000-constitution.md)：

```
需求 → REQ 编号 → 冻结契约 → 写测试（红）→ 实现（绿）→ 重构
     → 本地门禁 → push bugfix → CI 自动开 PR → Codex review
                                          ↓ FAIL：回 bugfix 修，重跑门禁
                                          ↓ PASS：系统测试 → --ff-only 合 development
                                                         → 人工 --no-ff 合 main → 打 tag
```

- Claude（开发 Agent）只操作 `bugfix` 分支，不手动开 PR
- CI 的 `open-pr` job 在门禁通过后自动开 PR 到 `development` 并 @ QA；`branch-guard` job 校验提升不变量
- `development` 由 Codex 操作（`--ff-only`），`main` 只接受人工合并（`--no-ff` + tag）
- 合并前用 `python scripts/check_promotion.py <源分支> <目标分支>` 自检（只读，不合并不推送）

---

## 文档

| 文档 | 内容 |
|---|---|
| [`spec/`](spec/README.md) | 规格索引：需求、架构、契约、并行开发拆分、测试策略 |
| [`docs/TECHNICAL.md`](docs/TECHNICAL.md) | 技术说明：分层细节、关键设计决策、数据库表、排障 |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | 运行手册：部署、启停、常见故障处理 |
| [`docs/TESTPLAN.md`](docs/TESTPLAN.md) | 测试计划：分层策略、用例清单、系统测试执行步骤 |

## 模型供应商

`AMR_LLM_PROVIDER` 四选一（`REQ-008`）：

| 值 | 说明 |
|---|---|
| `openai` | 任何 OpenAI 兼容网关（默认） |
| `oneapi` | OneAPI 网关 |
| `ollama` | 本地推理 |
| `fake` | 确定性桩模型，**仅供离线测试与系统测试**，不产生真实调用 |
