# 并行开发拆分（030）

目标：让 4 条工作流**真正同时开工**，并且在合并时**零冲突、零接口返工**。

并行的前提不是「人多」，而是**接口先冻结**。因此：

```
WS-0 地基（必须最先完成，且一次冻结）
  domain/  +  ports/  +  config.py  +  工程基建
        │
        ├──────────┬──────────┬──────────┬──────────┐
        ▼          ▼          ▼          ▼          ▼
      WS-A       WS-B       WS-C       WS-D       WS-E
      存储       接入异步    智能体      前端       工程文档
        │          │          │          │          │
        └──────────┴──────────┴──────────┴──────────┘
                          │
                          ▼
                   WS-F 集成与门禁（单点收口）
```

---

## 1. WS-0 地基（阻塞其他所有工作流，**先做且做完**）

| 产出 | 文件 | 冻结内容 |
|---|---|---|
| 领域内核 | `src/amr/domain/{models,status,inputs,errors}.py` | `020` 全部契约 |
| 端口声明 | `src/amr/ports/*.py` | `021` 全部签名 |
| 配置 | `src/amr/config.py` | `Settings` 字段名（其他工作流读配置的唯一入口） |
| 工程基线 | `pyproject.toml`、`Makefile`、`.gitignore`、`.env.example` | 门禁命令 |
| 测试脚手架 | `tests/conftest.py`、`tests/fakes/*` | 共享 Fake（`FrozenClock`、`MemoryJobRepository` 等） |

**冻结判据**：`make lint && make type && make test` 全绿，且 `ports/` 的签名与本文件 `021` 逐字一致。
**冻结之后**：任何签名变更走「改 spec → 通知 → 各方同步改」流程，不允许某一方偷偷改端口。

---

## 2. 工作流清单

### WS-A 存储适配（`adapters/mysql_repository.py`、`adapters/memory_repository.py`）

| 项 | 内容 |
|---|---|
| **产出** | `JobRepository` 的两个实现 + `adapters/system_clock.py`（`Clock`/`IdGenerator` 生产实现） |
| **契约测试** | `tests/contract/test_job_repository.py`（`021` §1.1 的 10 组用例，参数化跑两个实现） |
| **验收** | `pytest tests/contract -q` 全绿；MySQL 实现含建表 DDL 与「终态不可变」的条件 UPDATE |
| **依赖** | WS-0（`Job`、`JobStatus`、`JobRepository`） |
| **文件边界** | 只写 `src/amr/adapters/*_repository.py`、`adapters/system_clock.py`、`tests/contract/**` |

### WS-B 接入与异步（`api/`、`worker/`、队列适配）

| 项 | 内容 |
|---|---|
| **产出** | `api/{app,routes,schemas,deps}.py`、`worker/{tasks,bootstrap}.py`、`adapters/{celery_queue,inline_queue}.py`、`services/job_service.py`、`services/orchestrator.py` |
| **集成测试** | `tests/integration/test_api_submit.py`、`test_api_query.py`、`test_inline_e2e.py`（提交 → 执行 → 落库全链路，零外部依赖） |
| **验收** | `pytest tests/integration -q` 全绿；`create_app` 可注入全部依赖；422/404/503 三态均有用例 |
| **依赖** | WS-0；`services/` 由本工作流负责（它是 WS-B/WS-C 的交界，故划归 WS-B 并由 WS-C 复用） |
| **文件边界** | `src/amr/api/**`、`src/amr/worker/**`、`src/amr/services/**`、`src/amr/adapters/{celery_queue,inline_queue}.py` |

### WS-C 智能体编排（`crews/`、`adapters/crewai_runner.py`、`adapters/llm_factory.py`）

| 项 | 内容 |
|---|---|
| **产出** | 两个 Crew 的 `crew.py` + `config/{agents,tasks}.yaml`；`CrewAIFlowRunner`（`@start` 调研 → `@listen` 创作）；`llm_factory` 四供应商；`adapters/fake_runner.py` |
| **单元测试** | `tests/unit/test_crews_config.py`（YAML 可加载、变量占位符与 `CrewInputs` 字段一致、`output_json` 绑定正确、`copy` 任务 `context` 依赖前两个任务）、`tests/unit/test_llm_factory.py`（缺变量时报错信息点名变量） |
| **验收** | `pytest tests/unit -q` 全绿；`FakeFlowRunner` 返回合法 `FlowResult` 且可确定性复现 |
| **依赖** | WS-0（`FlowResult`、`ChatModel`、`LLMProvider`） |
| **文件边界** | `src/amr/crews/**`、`src/amr/adapters/{crewai_runner,fake_runner,llm_factory}.py` |

> WS-C 是**唯一**允许 import `crewai` 的工作流。这层隔离让 CrewAI 升级不波及业务代码（`010` §4.3）。

### WS-D 前端控制台（`src/web/**`）

| 项 | 内容 |
|---|---|
| **产出** | Vue 3 + TS + Vite 控制台：提交表单、3s 轮询、终态停轮询、URL 回填 `job_id`、事件流水、状态徽章、深色模式 |
| **契约** | 严格按 `022`；类型定义与响应字段一一对应 |
| **验收** | `npm run type-check` 通过；`npm run build` 成功；对着 `InlineTaskQueue` 起的后端手工走通一次 |
| **依赖** | `022`（**只依赖契约，不依赖后端实现**，因此可与 WS-A/B/C 完全并行） |
| **文件边界** | `src/web/**`（不碰后端任何文件） |

### WS-E 工程与文档（基建、CI、脚本）

| 项 | 内容 |
|---|---|
| **产出** | `.github/workflows/ci.yml`（门禁 + `open-pr` job）、`scripts/{dev_up,smoke}.sh`、`docs/TECHNICAL.md`、`docs/RUNBOOK.md`、`docs/TESTPLAN.md`、`README.md` |
| **验收** | CI 在 push 后真的跑起来并开 PR；`scripts/smoke.sh` 在真实中间件上跑通 |
| **依赖** | WS-0（门禁命令）；文档部分需要 WS-A/B/C 完成后回填 |
| **文件边界** | `.github/**`、`scripts/**`、`docs/**`、`README.md` |

> WS-E 与 WS-F 由**主协调者**承担（CI 与发布路径不适合并行改写）。

---

## 3. 冲突预防规则

1. **文件边界即所有权**：每个工作流只写自己那一列的文件；跨界需求先改 `031`/`021` 再动手。
2. **共享文件改动要串行**：`pyproject.toml`、`Makefile`、`tests/conftest.py`、`src/amr/config.py` 由主协调者统一改，其他工作流**只读**。
3. **禁止跨流 import**：WS-D 不 import 后端代码；WS-C 不 import `services/`；适配器之间不互相 import。
4. **契约优先**：实现中若发现 `021` 签名不够用，**先停下来改 spec**，而不是就地加参数——就地加参数会让其他三条流编译不过。
5. **合并顺序**：WS-0 → (WS-A ∥ WS-B ∥ WS-C ∥ WS-D) → 集成。WS-B 与 WS-C 有交界（`FlowExecutor` 调 `FlowRunner`），由 WS-B 定义 `FakeFlowRunner` 先行打通，WS-C 的真实实现后补。

---

## 4. 单点收口（WS-F：集成与门禁）

由主协调者执行，**并行工作流的产出只有过了这里才算完成**：

1. 合并各工作流产出到 `bugfix`。
2. 跑完整门禁：`make lint && make type && make test`。
3. 补**跨工作流的集成测试**（最易漏的是「WS-B 的 JobService」与「WS-C 的真实 Runner」之间的缝）。
4. 更新 `040` 追溯表与 `docs/`。
5. push `origin/bugfix` → CI 自动开 PR → 交棒 Codex。
6. Codex Finding 回传后，在 `bugfix` 上继续修，重跑门禁再 push（`000-constitution.md` §1）。
