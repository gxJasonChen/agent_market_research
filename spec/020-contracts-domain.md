# 领域契约（020）

`domain/` 是零外部依赖的内核：只允许 import 标准库与 `pydantic`。本文件是**唯一**定义模型与状态机的地方，代码与测试都以此为准。

---

## 1. 作业状态机（`domain/status.py`）

### 1.1 状态取值

| 状态 | 含义 | 终态 |
|---|---|---|
| `PENDING` | 已受理并落库，尚未被 Worker 取走 | 否 |
| `RUNNING` | Worker 已开始执行 Flow | 否 |
| `COMPLETE` | 执行成功，`result` 为结构化结果 | **是** |
| `ERROR` | 执行失败，`result` 为错误摘要 | **是** |

```python
class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    ERROR = "ERROR"

TERMINAL: frozenset[JobStatus] = frozenset({JobStatus.COMPLETE, JobStatus.ERROR})

ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING:  frozenset({JobStatus.RUNNING, JobStatus.ERROR}),
    JobStatus.RUNNING:  frozenset({JobStatus.COMPLETE, JobStatus.ERROR}),
    JobStatus.COMPLETE: frozenset(),
    JobStatus.ERROR:    frozenset(),
}
```

### 1.2 迁移规则（`can_transition(src, dst) -> bool`）

按顺序判定，**先命中先返回**：

1. `src` 是终态 → **False**（终态不可变，`REQ-004`）
2. `src is dst` → **True**（非终态重复写同一状态是幂等，记 no-op）
3. 其余 → `dst in ALLOWED_TRANSITIONS[src]`

**穷举真值表**（测试必须逐格覆盖，共 16 格）：

| src \ dst | PENDING | RUNNING | COMPLETE | ERROR |
|---|---|---|---|---|
| **PENDING** | ✅ 幂等 | ✅ | ❌ | ✅ |
| **RUNNING** | ❌ | ✅ 幂等 | ✅ | ✅ |
| **COMPLETE** | ❌ | ❌ | ❌ | ❌ |
| **ERROR** | ❌ | ❌ | ❌ | ❌ |

> **关键格**：`ERROR → COMPLETE` = ❌。这一格就是源项目「失败作业被误报成功」缺陷的防线，必须永远有测试守着。

### 1.3 非法迁移的后果

任何写入非法迁移的尝试 → 抛 `IllegalTransitionError`（`domain/errors.py`），**绝不静默覆盖、绝不吞掉**。
`JobService`（应用层）与各 `JobRepository` 实现（存储层）都调用同一个 `can_transition`，规则只有一份。

---

## 2. 数据模型（`domain/models.py`）

### 2.1 模型输出 Schema（Pydantic，供 CrewAI `output_json` 使用）

```python
class MarketStrategy(BaseModel):
    """营销战略"""
    name: str = Field(description="市场战略名称")
    tactics: list[str] = Field(description="市场战略中使用的战术清单")
    channels: list[str] = Field(description="市场战略中使用的渠道清单")
    kpis: list[str] = Field(description="关键绩效指标清单")

class CampaignIdea(BaseModel):
    """活动创意"""
    name: str = Field(description="活动创意名称")
    description: str = Field(description="活动创意说明")
    audience: str = Field(description="目标受众")
    channel: str = Field(description="投放渠道")

class Copy(BaseModel):
    """广告文案"""
    title: str = Field(description="标题")
    body: str = Field(description="正文")
```

> **与源项目的差异（破坏性）**：`tatics` → `tactics`（源项目拼写错误）、`KPIs` → `kpis`（统一 snake_case）。
> 本系统前后端一并重建，不做字段别名兼容；下游若有历史数据需自行迁移。

### 2.2 领域对象（frozen dataclass）

```python
@dataclass(frozen=True, slots=True)
class Event:
    timestamp: datetime   # 带时区
    data: str

@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    status: JobStatus
    customer_domain: str
    project_description: str
    result: str | None          # JSON 文本或错误摘要；终态前为 None
    created_at: datetime
    updated_at: datetime
    events: tuple[Event, ...] = ()
```

- `Job` 是不可变值对象：改状态通过 `JobRepository.transition` 产生新值，不在原地改。
- 用 dataclass 而非 pydantic：这两者是**内部值对象**，不参与 HTTP 编解码（HTTP 形状见 `022`），无需 pydantic 的运行时解析开销。

### 2.3 执行结果（`domain/models.py::FlowResult`）

```python
@dataclass(frozen=True, slots=True)
class FlowResult:
    research_report: str                     # 阶段一：调研报告原文
    strategy: MarketStrategy                 # 阶段二：结构化战略
    campaign_idea: CampaignIdea              # 阶段二：结构化创意
    copy: Copy                               # 阶段二：结构化文案

    def to_json(self) -> str: ...            # 序列化为 jobs.result 的 JSON 文本
    @classmethod
    def from_json(cls, raw: str) -> "FlowResult": ...   # 校验失败抛 SchemaValidationError
```

**`jobs.result` 的 JSON 形状（`COMPLETE` 时）**：

```json
{
  "research_report": "……",
  "strategy": { "name": "…", "tactics": ["…"], "channels": ["…"], "kpis": ["…"] },
  "campaign_idea": { "name": "…", "description": "…", "audience": "…", "channel": "…" },
  "copy": { "title": "…", "body": "…" }
}
```

> **与源项目的差异**：源项目的 `result` 是 CrewAI 返回对象经 `str()` 后的**自由文本**，下游无法解析。
> 本系统改为受 Schema 约束的 JSON 对象（`REQ-006`）。

---

## 3. 输入校验（`domain/inputs.py`）

```python
class CrewInputs(BaseModel):
    customer_domain: str = Field(min_length=3, max_length=255)
    project_description: str = Field(min_length=10, max_length=4000)
```

- 两个字段在 `mode="before"` 校验器中 `strip()` 首尾空白；strip 后再判长度。
- 纯空白字符串视为空 → 校验失败。
- 失败时抛 pydantic 的 `ValidationError`；HTTP 层翻译为 `422`（`022` §4），CrewAI 层不关心。

`CrewInputs` 是**输入校验的单一事实来源**：HTTP 层直接用它做请求体模型，不在路由里另写规则。

---

## 4. 领域异常（`domain/errors.py`）

| 异常 | 含义 | HTTP 映射（`022`） |
|---|---|---|
| `AmrError` | 本系统所有业务异常的基类 | 500 |
| `InvalidInputError` | 领域层校验失败 | 422 |
| `JobNotFoundError` | 作业不存在 | 404 |
| `IllegalTransitionError` | 非法状态迁移（含写终态） | 500（属编程错误/并发冲突，非用户错误） |
| `SchemaValidationError` | 模型输出不合 Schema | 500（作业本身转 `ERROR`） |
| `RepositoryUnavailableError` | 存储不可用 | 503 |
| `QueueUnavailableError` | 队列不可用 | 503 |
| `FlowExecutionError` | Flow 执行失败（含下游异常） | 500（作业转 `ERROR`） |

所有异常都携带 `message: str`，并支持 `context: dict[str, object]` 携带结构化上下文（如 `job_id`、字段名）用于日志。
**异常信息不得包含密钥**（`000` §7）。
