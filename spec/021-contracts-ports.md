# 端口契约（021）—— 并行开发的冻结面

**本文件是并行开发的地基。** 一旦冻结，WS-A / WS-B / WS-C / WS-D 各自基于这些签名开工，互不需要等对方的实现。

规则：
- 端口用 `typing.Protocol`（结构化子类型），**不用 ABC** —— 适配器不需要继承、不需要注册。
- `services/` **只能** import `ports/` 与 `domain/`；import 任何适配器或第三方框架 = 架构违规（`NFR-004`，由 `tests/unit/test_architecture.py` 守护）。
- 端口方法只做「声明」，不含默认实现与副作用。
- **改签名 = 破坏性变更**：必须先改本文件、通知所有工作流，再改实现。

---

## 1. 存储端口（`ports/repository.py`）

```python
class JobRepository(Protocol):
    """作业与事件的持久化。实现：MemoryJobRepository（测试）、MySQLJobRepository（生产）。"""

    def create(self, job: Job) -> None:
        """插入作业（含初始状态 PENDING）。job_id 已存在时抛 ValueError。"""

    def get(self, job_id: str) -> Job | None:
        """返回作业及其事件（按时间正序）。不存在返回 None。"""

    def append_event(self, job_id: str, data: str, *, at: datetime) -> None:
        """追加一条事件。作业不存在抛 JobNotFoundError（不隐式建作业）。"""

    def transition(
        self,
        job_id: str,
        to: JobStatus,
        *,
        result: str | None = None,
        at: datetime,
    ) -> Job:
        """状态迁移，返回迁移后的作业。

        必须遵守 020 §1.2 的迁移规则（调用 domain.status.can_transition，规则只有一份）：
          - 作业不存在            → JobNotFoundError
          - 迁移非法（含写终态）  → IllegalTransitionError
          - 同一非终态重复写入    → 幂等成功（result 若传入则更新）
        实现还须在存储层做条件更新（MySQL: `UPDATE ... WHERE status NOT IN ('COMPLETE','ERROR')`
        并校验 affected rows），保证并发下终态不被覆盖。存储不可用 → RepositoryUnavailableError。
        """

    def list_recent(self, limit: int = 20) -> list[Job]:
        """按 created_at 倒序返回最近的作业（不含 events，避免 N+1）。"""
```

### 1.1 契约测试要求

同一套用例（`tests/contract/test_job_repository.py`）必须跑过**每一个**实现，参数化注入：

| 用例组 | 断言 |
|---|---|
| 创建与读取 | `create` 后 `get` 得到等值作业，状态 `PENDING`，`events == ()` |
| 重复创建 | 同 `job_id` 二次 `create` 抛 `ValueError` |
| 读取不存在 | `get` 返回 `None`（**不是**抛异常） |
| 事件追加 | 事件按时间正序返回；对不存在的作业追加抛 `JobNotFoundError` |
| 合法迁移 | `PENDING→RUNNING→COMPLETE` 逐步 `get` 一致，`result` 正确 |
| **终态不可变** | `ERROR → COMPLETE` 抛 `IllegalTransitionError`，且**库里仍是 ERROR** |
| 幂等 | `PENDING → PENDING` 返回成功且无异常 |
| 非法迁移 | `RUNNING → PENDING` 抛 `IllegalTransitionError` |
| 不存在作业迁移 | 抛 `JobNotFoundError` |
| 最近列表 | `list_recent` 按 `created_at` 倒序且遵守 `limit` |

---

## 2. 队列端口（`ports/queue.py`）

```python
class TaskQueue(Protocol):
    """把作业投递给执行方。实现：CeleryTaskQueue（生产）、InlineTaskQueue（集成测试）。"""

    def enqueue_flow(self, job_id: str, inputs: CrewInputs) -> None:
        """投递一次 Flow 执行。投递失败（broker 不可达）抛 QueueUnavailableError。

        语义：投递成功 ≠ 执行成功。调用方（JobService）在投递失败时必须把作业
        置为 ERROR —— 否则会留下永远停在 PENDING 的僵尸作业（REQ-011）。
        """
```

`InlineTaskQueue`（集成测试用）在同一进程内**同步**调用 `FlowExecutor.execute(job_id, inputs)`，使「提交 → 执行 → 落库」整条链路可在无 Redis 环境下断言。

---

## 3. 执行端口（`ports/runner.py`）

```python
class EventSink(Protocol):
    """执行期事件的落点。CrewAI Task 回调、阶段开始/结束都走这里。"""
    def emit(self, data: str) -> None: ...

class FlowRunner(Protocol):
    """跑一次完整业务流程。实现：CrewAIFlowRunner（生产）、FakeFlowRunner（测试）。"""

    def run(self, job_id: str, inputs: CrewInputs, sink: EventSink) -> FlowResult:
        """同步执行两阶段 Flow（调研 → 创作），返回结构化结果。

        必须：
          - 每个 Task 完成时通过 sink.emit() 写入其原始产出（REQ-005）
          - 模型输出不合 Schema 时抛 SchemaValidationError（REQ-006）
          - 任何下游异常包装为 FlowExecutionError 抛出（不吞异常）
        不得：直接读写数据库、直接改作业状态 —— 那是 JobService 的职责。
        """

class FlowExecutor(Protocol):
    """Worker 侧的一次执行单元：从「作业已入队」推进到「终态已落库」。"""
    def execute(self, job_id: str, inputs: CrewInputs) -> None: ...
```

### 3.1 `FlowExecutor` 的责任边界（`services/orchestrator.py`）

```
execute(job_id, inputs):
    job_service.mark_running(job_id)          # PENDING → RUNNING
    try:
        result = runner.run(job_id, inputs, sink)   # sink 落到 repository.append_event
        job_service.finish_success(job_id, result)  # RUNNING → COMPLETE
    except Exception as exc:
        job_service.finish_error(job_id, exc)       # RUNNING → ERROR，然后 **return**
        # 绝不 fallthrough 到 finish_success —— 这就是源项目缺陷的形状（REQ-004）
```

**实现必须写成「异常分支带 `return`，成功路径在 `try` 之外/之后单走」**，不得写成两个 `if/else` 并列的写法。
`tests/unit/test_orchestrator.py` 必须有一个用例：`runner.run` 抛异常后，仓储中状态是 `ERROR` 且**从未**出现 `COMPLETE`。

---

## 4. 模型端口（`ports/llm.py`）

```python
@runtime_checkable
class ChatModel(Protocol):
    """CrewAI Agent 可用的对话模型（结构化子类型，crewai.LLM 天然满足）。"""
    def call(self, messages: Any, tools: Any = None, callbacks: Any = None, **kwargs: Any) -> Any: ...
    def supports_function_calling(self) -> bool: ...

class LLMProvider(Protocol):
    """按配置构造模型客户端。实现见 adapters/llm_factory.py。"""
    def build(self) -> ChatModel: ...
```

支持的四类供应商（`REQ-008`）：

| 名称 | 凭据来源（环境变量） | 说明 |
|---|---|---|
| `openai` | `OPENAI_API_BASE` / `OPENAI_API_KEY` / `OPENAI_MODEL_NAME` | OpenAI 兼容网关，**默认** |
| `oneapi` | `ONEAPI_API_BASE` / `ONEAPI_API_KEY` / `ONEAPI_MODEL_NAME` | OneAPI 网关 |
| `ollama` | `OLLAMA_API_BASE` / `OLLAMA_API_KEY` / `OLLAMA_MODEL_NAME` | 本地推理，api_key 默认 `ollama` |
| `fake` | 无 | 确定性桩模型，供离线测试与系统测试使用（**本系统新增**） |

- 缺少必填环境变量时抛 `InvalidInputError`，信息里点明**缺哪个变量**（不含其值）。
- `fake` 模型：对固定输入返回**固定输出**（含合法 JSON 片段），保证同一输入两次运行结果一致。

---

## 5. 时间与标识端口（`ports/clock.py`）

```python
class Clock(Protocol):
    def now(self) -> datetime:
        """带时区的当前时间（UTC）。"""

class IdGenerator(Protocol):
    def new_id(self) -> str:
        """新的作业 ID（生产实现为 UUID4 字符串）。"""
```

业务代码**不得**直接 `datetime.now()` / `uuid4()`（测试无法复现）。生产实现在 `adapters/system_clock.py`；测试用 `FrozenClock` / `SeqIdGenerator`。

---

## 6. 事件落点端口（`ports/events.py`）

```python
class EventPublisher(Protocol):
    """把执行期事件写进作业流水。实现：RepositoryEventPublisher。"""
    def publish(self, job_id: str, data: str) -> None: ...
```

- `EventSink`（§3）是**执行期句柄**（已绑定 `job_id`），`EventPublisher` 是**落点**。
  `RepositoryEventPublisher.sink(job_id)` 返回绑定好的 `EventSink`，`FlowRunner` 只拿到 `sink`，不知道存储存在。
- 事件写入失败**不得**中断整条 Flow（记 `ERROR` 级日志后继续）—— 可观测性故障不应导致业务失败。

---

## 7. 装配点（谁把谁接起来）

依赖注入只发生在两个地方，业务代码里**不得**出现 `if 生产/测试` 分支：

| 装配点 | 文件 | 组装内容 |
|---|---|---|
| HTTP 进程 | `api/app.py::create_app(settings, repository, queue, clock, ids)` | 纯函数工厂，测试可直接注入内存实现 |
| Worker 进程 | `worker/bootstrap.py::build_executor(settings)` | 按 `Settings` 选择 MySQL / Celery / 真实 LLM |
| Worker 任务 | `worker/tasks.py` | Celery app 与任务声明；任务体只调 `FlowExecutor.execute` |

`create_app` 必须支持**全部依赖作为参数传入**（无参调用时按 `Settings` 装配生产实现），这是集成测试能做到「零外部依赖」的关键。
