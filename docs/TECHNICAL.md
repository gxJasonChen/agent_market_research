# 技术文档

面向接手者：**为什么这么设计**、代码在哪、出问题从哪儿查。
行为契约以 [`spec/`](../spec/README.md) 为准，本文件解释实现。

---

## 1. 一句话架构

```
HTTP 接入(FastAPI)  →  队列(Redis/Celery)  →  执行(Worker + CrewAI Flow)  →  存储(MySQL)
        ↑                                                                        ↓
        └────────────────────── 轮询查询作业状态与事件流水 ────────────────────────┘
```

业务上只有两个接口：`POST /api/crew` 提交、`GET /api/crew/{job_id}` 查询。
提交不等待推理，只负责「落库 + 入队」，推理完全在独立 Worker 进程里跑。

---

## 2. 分层与依赖方向

```
┌───────────────────────────────────────────────────────────────┐
│ 接入层   src/amr/api/          src/amr/worker/                │
│          routes / schemas /      tasks（Celery 任务声明）      │
│          app（纯函数工厂）        bootstrap（按配置装配）       │
└──────────────┬─────────────────────────┬──────────────────────┘
               ▼                         ▼
┌───────────────────────────────────────────────────────────────┐
│ 应用层   src/amr/services/                                    │
│          job_service.py   作业状态机的**唯一写入口**            │
│          orchestrator.py  一次执行的推进与收尾                  │
└──────────────┬─────────────────────────▲──────────────────────┘
               │ 依赖 Protocol            │ 实现 Protocol
               ▼                         │
┌──────────────────────────┐  ┌──────────┴────────────────────┐
│ 端口层   src/amr/ports/  │  │ 适配层   src/amr/adapters/     │
│  JobRepository TaskQueue │◄─┤  mysql_repository memory_*     │
│  FlowRunner  ChatModel   │  │  celery_queue    inline_queue  │
│  Clock IdGenerator ...   │  │  crewai_runner   fake_runner   │
└──────────────┬───────────┘  │  llm_factory     repository_*  │
               ▼              └───────────────────────────────┘
┌───────────────────────────────────────────────────────────────┐
│ 领域层   src/amr/domain/   零外部依赖（除 pydantic / 标准库）   │
│          status(状态机) models(Schema) inputs errors          │
└───────────────────────────────────────────────────────────────┘
```

**铁律**：箭头只能向内。`services/` 不 import 任何适配器、`crewai`、`celery`、`mysql.connector`、`redis`。
这条由 `tests/unit/test_architecture.py` 用 AST 扫描守护 —— 写在文档里的架构约束如果没人守，几周后就不成立了。

---

## 3. 几个关键设计（以及为什么）

### 3.1 端口 + 依赖倒置：为了能测

源项目的 `utils/jobManager.py` 在**模块导入期**就连 MySQL 并建表，`tasks.py` 在顶层构造 Celery 实例。
后果是：任何测试都得先有真实 MySQL 与 Redis，于是它的 `tests/` 目录一直是空的。

本系统把「用哪个存储/队列/模型」推到进程装配点（`api/app.py::create_app` 与 `worker/bootstrap.py::build_executor`），
业务代码只认 Protocol。换来三件事：

- 单元测试注入内存替身 → 毫秒级、离线可跑（`NFR-001`）；
- 契约测试同一套用例跑内存与 MySQL 两个实现，保证语义一致；
- 换存储/换队列不动业务代码。

### 3.2 状态机：三层防护，因为源项目在这里翻过车

源项目的严重缺陷：Flow 抛异常时写了 `ERROR`，随后**成功分支无条件又写一次 `COMPLETE`**（此时结果为 `None`），
调用方拿到假的「完成」。根因不是某一行的疏忽，而是**没有唯一的状态写入口**——
两个分支各写各的，谁也不知道对方写过什么。

本系统的防护分三层：

| 层 | 位置 | 作用 |
|---|---|---|
| 规则 | `domain/status.py::can_transition` | 迁移真值表只有一份；终态对任何目标都返回 False |
| 写入口 | `services/job_service.py` | 全系统只有它能改状态；调用上面的规则 |
| 存储 | `adapters/mysql_repository.py::transition` | 条件 `UPDATE ... WHERE status NOT IN ('COMPLETE','ERROR')` + affected rows 校验，堵住并发窗口 |

并且 `services/orchestrator.py` 的异常分支**收尾后立刻 `return`**，成功路径写在 `try` 之外——
结构上不给「失败后又走成功分支」留位置。回归测试见 `TC-U-002 / TC-U-012 / TC-U-013 / TC-S-002`。

### 3.3 Flow 与 Crew 的边界

`services/orchestrator.py` 只做「调 Runner → 拿结果 → 交给 JobService」。
CrewAI 的 Flow（`@start` / `@listen`）是**适配细节**，落在 `adapters/crewai_runner.py` + `crews/`。

好处：编排逻辑能脱离 CrewAI 测试（`FakeFlowRunner`），CrewAI 升级不会污染业务层。
`crewai` 只在 `adapters/` 与 `crews/` 里出现。

### 3.4 时间与随机性注入

`Clock` / `IdGenerator` 是端口。生产用 `SystemClock`（UTC aware）与 `Uuid4Generator`，测试用 `FrozenClock` / `SeqIdGenerator`。
直接 `datetime.now()` 会让断言退化成「大概是现在」，那种测试基本没有价值。

时间一律 **tz-aware UTC**：源项目存 naive 时间，前端按本地时区渲染，跨时区会整体偏移。

### 3.5 事件写入失败不拖垮业务

`RepositoryEventPublisher.publish` 捕获所有异常只记日志。
事件流水是可观测性设施；它坏掉时，一次已经成功的推理不该因此变成失败作业（`REQ-005`）。
这是本项目**唯一**一处刻意吞异常的地方，代码里有注释说明。

### 3.6 结构化输出必须过校验

模型输出是不可信数据。`FlowResult.from_json` 是唯一入口，校验不过抛 `SchemaValidationError`，
作业转 `ERROR` 并把「哪个模型、哪个字段」写进错误摘要。
绝不允许把裸文本塞进 `result` 冒充结构化结果（源项目就是这么干的，下游无法消费）。

---

## 4. 数据模型

### 4.1 输出 Schema（`domain/models.py`）

```python
MarketStrategy(name, tactics[], channels[], kpis[])   # 营销战略
CampaignIdea(name, description, audience, channel)    # 活动创意
Copy(title, body)                                     # 广告文案
```

三个模型都设了 `extra="forbid"`：模型多吐字段时宁可失败，也不要静默吞掉。

> 与源项目的破坏性差异：`tatics` → `tactics`（源项目拼写错误）、`KPIs` → `kpis`。

### 4.2 结果 JSON（`COMPLETE` 时的 `jobs.result`）

```json
{
  "research_report": "……",
  "strategy": { "name": "…", "tactics": ["…"], "channels": ["…"], "kpis": ["…"] },
  "campaign_idea": { "name": "…", "description": "…", "audience": "…", "channel": "…" },
  "copy": { "title": "…", "body": "…" }
}
```

### 4.3 MySQL 表

```sql
CREATE TABLE IF NOT EXISTS jobs (
  job_id              VARCHAR(64)  PRIMARY KEY,
  status              VARCHAR(16)  NOT NULL,          -- PENDING/RUNNING/COMPLETE/ERROR
  customer_domain     VARCHAR(255) NOT NULL,
  project_description TEXT         NOT NULL,
  result              MEDIUMTEXT   NULL,              -- COMPLETE: JSON 文本；ERROR: 错误摘要
  created_at          DATETIME(6)  NOT NULL,
  updated_at          DATETIME(6)  NOT NULL
) ENGINE=InnoDB CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS events (
  id        BIGINT AUTO_INCREMENT PRIMARY KEY,
  job_id    VARCHAR(64) NOT NULL,
  timestamp DATETIME(6) NOT NULL,
  data      TEXT        NOT NULL,
  KEY idx_events_job (job_id, id),
  CONSTRAINT fk_events_job FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
) ENGINE=InnoDB CHARSET=utf8mb4;
```

表由仓储在首次使用时自动建（`IF NOT EXISTS`），无需手工 DDL。
时间列存 UTC，读出后重新挂上 UTC 时区。

---

## 5. 代码索引

| 想改什么 | 去哪 |
|---|---|
| 状态取值与迁移规则 | `src/amr/domain/status.py` |
| 输出 Schema / 结果 JSON 形状 | `src/amr/domain/models.py` |
| 入参校验规则 | `src/amr/domain/inputs.py` |
| 状态迁移的写入口 | `src/amr/services/job_service.py` |
| 一次执行的推进与收尾 | `src/amr/services/orchestrator.py` |
| HTTP 路由与错误映射 | `src/amr/api/routes.py`、`api/app.py` |
| Celery 任务与装配 | `src/amr/worker/tasks.py`、`worker/bootstrap.py` |
| MySQL 读写与终态防护 | `src/amr/adapters/mysql_repository.py` |
| CrewAI Flow 与两阶段编排 | `src/amr/adapters/crewai_runner.py` |
| Agent / Task 的角色与 Prompt | `src/amr/crews/*/config/*.yaml` |
| 模型供应商适配 | `src/amr/adapters/llm_factory.py` |
| 检索工具（哪个 Agent 挂哪些工具） | `src/amr/crews/tools.py` |
| 配置项 | `src/amr/config.py` + `.env.example` |

### 5.1 `AMR_LLM_PROVIDER=fake` 为什么换的是执行器

它不是「假模型」，而是把 `FlowRunner` 换成 `FakeFlowRunner`（确定性、零网络）。

原因（实测，不是猜测）：CrewAI 1.15 的 `LLM` 把 `call` 委托给 provider 实现——
写一个 `LLM` 子类覆写 `call` 会被绕过，仍然去连真实端点；纯鸭子类型的桩则连
`Agent(llm=...)` 的 pydantic 校验都过不了（只接受 `str | BaseLLM`）。
所以与其造一个「假模型」让测试去测桩本身，不如在装配层承认这件事。

覆盖范围：HTTP → JobService → Celery → Worker → FlowExecutor → Repository 全链路。
**不覆盖** CrewAI 自身行为——那由真实网关的系统测试（`--run-real-llm`）负责。

---

## 6. 可观测性

- **事件流水**：每次状态变更与每个 Task 的产出都落 `events` 表，按时间正序可查。
  排查「Agent 在哪一步跑偏」时，先看这个，比加 `print` 有效。
- **日志**：结构化，带 `job_id`。`AMR_LOG_LEVEL` 控制级别。
- **CrewAI Traces**：设 `CREWAI_TRACING_ENABLED=true` 开启，可下钻到 Agent 决策、工具调用参数与 Token 消耗。

---

## 7. 已知取舍与后续改进

| 项 | 现状 | 后续 |
|---|---|---|
| 作业幂等 | 提交无幂等键 | 可加 `Idempotency-Key` |
| 作业取消 / 断点续跑 | 不支持 | 需要 Flow 状态持久化 |
| 鉴权与限流 | 无 | 接口层加 |
| 存储连接 | 每次操作新建连接 | 换连接池（`mysql.connector.pooling`） |
| 检索工具失败重试 | 交给 CrewAI | 可在工具层包一层重试与降级 |
| 结果缓存 | 无 | 相同输入可考虑复用 |
