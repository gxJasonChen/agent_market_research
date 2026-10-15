# 架构规格（010）

## 1. 分层与依赖方向

```
┌──────────────────────────────────────────────────────────────┐
│  接入层   api/ (FastAPI)          worker/ (Celery tasks)      │
│           · HTTP 解析与校验        · 从队列取作业并执行        │
└───────────────┬──────────────────────────┬───────────────────┘
                │                          │
                ▼                          ▼
┌──────────────────────────────────────────────────────────────┐
│  应用层   services/                                           │
│           · JobService：作业状态机（唯一有权改状态的地方）      │
│           · FlowOrchestrator：两阶段 Crew 编排                 │
└───────────────┬──────────────────────────▲───────────────────┘
                │ 依赖 Protocol            │ 实现 Protocol
                ▼                          │
┌──────────────────────────┐   ┌───────────┴───────────────────┐
│  端口层   ports/         │   │  适配层   adapters/            │
│  JobRepository           │◄──┤  mysql_repository / memory_*   │
│  TaskQueue               │   │  celery_queue / inline_queue   │
│  FlowRunner              │   │  crewai_runner / fake_runner   │
│  LLMProvider             │   │  llm_factory（openai/oneapi/   │
│  EventSink               │   │              ollama/fake）     │
└───────────────┬──────────┘   └───────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────────────┐
│  领域层   domain/   （零外部依赖，除 pydantic / 标准库）        │
│  models / status（状态机）/ inputs（校验）/ errors            │
└──────────────────────────────────────────────────────────────┘
```

**铁律**：箭头只能向内。`services/` 只认 `ports/` 的 Protocol，不 import 任何适配器、`crewai`、`celery`、`mysql.connector`、`redis`。由 `tests/unit/test_architecture.py` 静态守护（`NFR-004`）。

---

## 2. 运行时拓扑

```
                     POST /api/crew                    GET /api/crew/{job_id}
调用方 ─────────────────────────────► FastAPI (:8012) ◄────────────── 调用方
   ▲                                    │  │
   │ 202 {job_id}                       │  │ 1. JobService.create() → PENDING 落库
   │                                    │  │ 2. TaskQueue.enqueue_flow(job_id, inputs)
   │                                    ▼  ▼
   │                              Redis（Celery broker）
   │                                       │
   │                                       ▼
   │                              Celery Worker（可多副本）
   │                                       │ 3. JobService.mark_running()
   │                                       ▼
   │                              FlowRunner（CrewAI Flow）
   │                              ┌────────┴─────────┐
   │                              │ @start 调研 Crew │ 4. 每个 Task 完成 → EventSink.emit()
   │                              │      ↓           │     → JobRepository.append_event()
   │                              │ @listen 创作 Crew│
   │                              └────────┬─────────┘
   │                                       ▼ 5. JobService.finish_success / finish_error
   └──── MySQL（jobs / events）◄───────────┘    （终态不可变，REQ-004）
```

---

## 3. 组件职责

| 组件 | 职责 | 明确不做 |
|---|---|---|
| `api/` | HTTP 编解码、输入合法性、状态码映射 | 不写业务规则、不碰数据库 |
| `worker/` | Celery app 与任务声明、进程级装配（依赖注入） | 不含状态机逻辑 |
| `services/job_service.py` | **作业状态机唯一入口**：create / mark_running / finish_success / finish_error | 不 import 框架、不碰 SQL |
| `services/orchestrator.py` | 触发 Flow、把结果转成状态变更 | 不知道队列与 HTTP |
| `domain/` | 数据模型、状态迁移表、输入校验规则、领域异常 | 零外部依赖 |
| `ports/` | Protocol 声明（依赖倒置的接缝） | 无实现、无副作用 |
| `adapters/` | Protocol 的具体实现（MySQL / Redis / CrewAI / LLM） | 不含业务判断 |
| `crews/` | CrewAI 声明式配置（YAML）与 Crew 组装 | 不写状态、不写存储 |

---

## 4. 关键设计决策

### 4.1 为什么用端口 + 依赖倒置

源项目在模块导入期就 `mysql.connector.connect()` 并建表，`tasks.py` 顶层构造 Celery 实例——后果是**任何测试都必须先有真实 MySQL 与 Redis**，`tests/` 因此一直是空目录。
本设计把「用哪个存储/队列/模型」推迟到进程装配点（`api/app.py` 的 factory 与 `worker/tasks.py` 的 bootstrap），业务代码只认 Protocol，于是：

- 单元测试注入内存 Fake → 毫秒级、离线可跑（`NFR-001`）；
- 契约测试同一套用例跑多个实现（内存 / MySQL），保证语义一致；
- 换存储不动业务代码。

### 4.2 状态机放在哪

状态迁移表在 `domain/status.py`（纯函数，可穷举测试）；**唯一写入口**是 `JobService`；仓储层再做一次「终态不可变」的数据库级防护（条件 UPDATE + 行数校验）。
三层防护的理由：源项目的缺陷正是「两个分支各自写状态、互不知情」，只靠调用方自觉必然复发。
**拒绝非法迁移时抛 `IllegalTransitionError`，绝不静默覆盖**（`REQ-004`）。

### 4.3 Flow 与 Crew 的边界

`services/orchestrator.py` 只负责「调用 FlowRunner → 拿结果 → 交给 JobService」；CrewAI 的具体 Flow（`@start` / `@listen`）属于**适配细节**，放在 `adapters/crewai_runner.py` + `crews/`。
好处：编排逻辑可以脱离 CrewAI 测试（`FakeFlowRunner`），CrewAI 升级不会污染业务层。

### 4.4 时间与随机性注入

`job_id` 生成与时间戳通过端口注入（`IdGenerator` / `Clock`），使测试可复现、断言确定（`000-constitution.md` §6：单测不得依赖当前时间）。

---

## 5. 并行开发切分（详见 `030-workstreams.md`）

| 工作流 | 范围 | 依赖 |
|---|---|---|
| WS-0 地基 | `domain/`、`ports/`、`config.py`、工程基建 | 无（**先冻结**） |
| WS-A 存储 | `adapters/mysql_*`、`adapters/memory_*` + 契约测试 | WS-0 |
| WS-B 接入与异步 | `api/`、`worker/`、队列适配 + 集成测试 | WS-0 |
| WS-C 智能体编排 | `crews/`、`adapters/crewai_*`、`llm_factory` | WS-0 |
| WS-D 前端 | `src/web/**` | `022` HTTP 契约 |
| WS-E 工程与文档 | `pyproject`、`Makefile`、`.github/`、`docs/`、`scripts/` | WS-0 |

WS-A / B / C / D / E 之间**互不 import**，只通过 `021` 的 Protocol 与 `022` 的 HTTP 契约通信 —— 因此可真正并行。

---

## 6. 技术选型

| 项 | 选型 | 理由 |
|---|---|---|
| 语言 | Python 3.12 | 与源项目一致；`StrEnum`、`X | Y` 语法可用 |
| 依赖管理 | uv | 源项目已用，锁文件可复现 |
| Web | FastAPI + uvicorn | 与源项目一致 |
| 编排 | CrewAI Flow + Crew | 与源项目一致 |
| 异步 | Celery + Redis | 与源项目一致；broker 持久化、可扩容 |
| 存储 | MySQL 8 | 与源项目一致 |
| 校验 | Pydantic v2 | 与源项目一致 |
| 测试 | pytest + pytest-cov + httpx | 事实标准 |
| 静态检查 | ruff（lint + format）+ mypy | 快、零争议 |
| 前端 | Vue 3 + TS + Vite | 与源项目一致 |

---

## 7. 目录结构（目标态）

```
agent_market_research/
├── spec/                     规格（本目录）
├── docs/                     交付文档：TECHNICAL / RUNBOOK / TESTPLAN
├── src/
│   ├── amr/                  后端包
│   │   ├── config.py         Settings（pydantic-settings）
│   │   ├── domain/           models.py status.py inputs.py errors.py
│   │   ├── ports/            repository.py queue.py runner.py llm.py clock.py
│   │   ├── adapters/         mysql_repository.py memory_repository.py
│   │   │                     celery_queue.py inline_queue.py
│   │   │                     crewai_runner.py fake_runner.py
│   │   │                     llm_factory.py
│   │   ├── services/         job_service.py orchestrator.py
│   │   ├── api/              app.py routes.py schemas.py deps.py
│   │   ├── worker/           tasks.py bootstrap.py
│   │   └── crews/            market_analyst/ content_creator/
│   │                         （各含 crew.py + config/{agents,tasks}.yaml）
│   └── web/                  Vue 3 前端
├── tests/                    unit/ contract/ integration/ system/
├── scripts/                  启动与冒烟脚本
├── .github/workflows/ci.yml  门禁 + 自动开 PR
├── pyproject.toml  Makefile  .env.example  README.md
```
