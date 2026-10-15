# 测试策略（040）

## 1. 四层金字塔

| 层 | 位置 | 依赖 | 速度 | 数量占比 |
|---|---|---|---|---|
| 单元 | `tests/unit/` | 内存 Fake + 纯函数 | ms | ~65% |
| 契约 | `tests/contract/` | 真实 MySQL（同一套用例也跑内存实现） | 百 ms | ~10% |
| 集成 | `tests/integration/` | FastAPI TestClient + 内存适配器 + Celery eager | 百 ms | ~20% |
| 系统 | `tests/system/` | 真 MySQL + 真 Redis + 真 Worker + 真网关 | 秒~分钟 | ~5% |
| 前端 | `src/web/src/**/*.spec.ts` | vitest（node 环境） | ms | 随前端代码 |

前端是独立技术栈，用 vitest 原地测试（`npm run test`），不进 pytest 门禁；
它的**交付可用性**由 `TC-S-005` 在系统层把关（type-check + build + 接口路径）。

**离线红线（`NFR-001`）**：前三层**必须**能在无网络、无密钥、无 MySQL/Redis 的机器上全绿。这是 CI 的前提，也是 TDD 红绿循环能跑得动的前提。

```
make test        →  单元 + 契约 + 集成（离线可跑，CI 门禁）
make test-system →  系统测试（需 docker/本机中间件；真模型用 @pytest.mark.real_llm 单独开关）
```

---

## 2. 用例矩阵

用例编号规则：`TC-<层>-<序号>`。**每个用例的 docstring 必须写它守护的 `REQ-xxx`。**

### 2.1 单元层

| 编号 | 用例 | 守护 | 关键断言 |
|---|---|---|---|
| TC-U-001 | 状态迁移真值表穷举（16 格） | REQ-004 | 逐格比对 `020` §1.2 真值表 |
| TC-U-002 | `ERROR → COMPLETE` 被拒 | REQ-004 | 抛 `IllegalTransitionError`（**源项目缺陷的回归测试**） |
| TC-U-003 | 终态任意写入被拒 | REQ-004 | `COMPLETE`/`ERROR` 作为源状态时全部返回 False |
| TC-U-004 | 非终态幂等 | REQ-004 | `PENDING→PENDING`、`RUNNING→RUNNING` 为 True |
| TC-U-005 | `CrewInputs` 校验：长度边界 | REQ-010 | 2/3/255/256 与 9/10/4000/4001 四个边界 |
| TC-U-006 | `CrewInputs` 校验：空白与 strip | REQ-010 | `"  abc  "` 通过且值为 `"abc"`…；纯空白拒绝 |
| TC-U-007 | `FlowResult` JSON 往返 | REQ-006 | `to_json` → `from_json` 等值 |
| TC-U-008 | `FlowResult.from_json` 字段缺失/类型错 | REQ-006 | 抛 `SchemaValidationError`，信息含字段名 |
| TC-U-009 | `JobService.create` | REQ-001 | 落库状态 `PENDING`、`result is None`、写了一条事件 |
| TC-U-010 | `JobService.create` 入队失败 | REQ-011 | 作业被置 `ERROR` + 抛 `QueueUnavailableError`（**不留僵尸 PENDING**） |
| TC-U-011 | `JobService.finish_error` | REQ-004 | 状态 `ERROR`、结果为错误摘要 |
| TC-U-012 | `JobService.finish_success` 在 ERROR 之后 | REQ-004 | 抛 `IllegalTransitionError`，状态仍是 `ERROR` |
| TC-U-013 | `FlowExecutor` 异常路径 | REQ-004 | runner 抛异常 → 状态 `ERROR`，**且从未出现 `COMPLETE`**（用记录型 Fake 断言写入序列） |
| TC-U-014 | `FlowExecutor` 成功路径 | REQ-003 | 状态 `COMPLETE`，`result` 可 `from_json` |
| TC-U-015 | `FlowExecutor` 先 mark_running | REQ-004 | 写入序列为 `RUNNING → COMPLETE` |
| TC-U-016 | `RepositoryEventPublisher` 吞掉存储故障 | REQ-005 | 仓储抛异常时不向上传播，只记日志 |
| TC-U-017 | Crews YAML 可加载且占位符合法 | REQ-007 | 所有 `{...}` 占位符 ⊆ `CrewInputs` 字段名 |
| TC-U-018 | 结构化任务绑定 `output_json` | REQ-006 | 战略/创意/文案三个 Task 绑定对应模型 |
| TC-U-019 | 文案任务显式 `context` 依赖 | REQ-007 | `context` 含战略与创意两个 Task |
| TC-U-020 | `llm_factory` 缺变量报错 | REQ-008/009 | 异常信息含变量名、**不含变量值** |
| TC-U-021 | `llm_factory` 四供应商分支 | REQ-008 | `openai`/`oneapi`/`ollama`/`fake` 均可构造 |
| TC-U-022 | 架构守护：分层依赖只能向内 | NFR-004 | AST 扫描每一层的 import：`services` 不得引适配器/框架，`domain` 不得引任何本项目层 |
| TC-U-023 | 架构守护：`domain` 零外部依赖 | NFR-004 | `domain/**` 只允许标准库 + pydantic；扫描器自身有用例证明不是「永远通过」 |
| TC-U-024 | 装配点按 `AMR_LLM_PROVIDER` 分派执行器 | REQ-008 | `fake` → `FakeFlowRunner`；真实供应商缺配置时**装配期**就报错，不拖到首次调用 |
| TC-U-025 | 装配不触网 | REQ-009 | 构造仓储不连库（给一个不可达地址也必须构造成功）；Worker 侧队列一用就报错 |

### 2.2 契约层

| 编号 | 用例 | 守护 | 说明 |
|---|---|---|---|
| TC-C-001..010 | `021` §1.1 的 10 组 | REQ-004/005 | 参数化跑 `MemoryJobRepository` 与 `MySQLJobRepository`，**同一套断言** |

> 契约层是「两个实现语义一致」的保证。MySQL 实现里那句「终态不可变」的条件 UPDATE 只有在真实数据库上才验得出来，所以这一层必须连真库。

### 2.3 集成层

| 编号 | 用例 | 守护 | 关键断言 |
|---|---|---|---|
| TC-I-001 | 提交作业返回 202 + `job_id` | REQ-001 | 状态码、响应体、`job_id` 为 UUID |
| TC-I-002 | 提交接口 P95 延迟 | NFR-005 | 20 次调用 P95 < 200ms（内存实现） |
| TC-I-003 | 入参非法 → 422 | REQ-010 | 三个非法样例（过短、空白、超长） |
| TC-I-004 | 查询不存在 → 404 | REQ-003 | `detail` 存在 |
| TC-I-005 | 查询已存在 → 200 结构完整 | REQ-003 | 四个字段齐全、`events` 正序 |
| TC-I-006 | 队列不可用 → 503 且作业为 ERROR | REQ-011 | 注入抛错的队列 Fake |
| TC-I-007 | 存储不可用 → 503 | REQ-011 | 注入抛错的仓储 Fake |
| TC-I-008 | 端到端（同步执行） | REQ-002/003/006 | `InlineTaskQueue` + `FakeFlowRunner`：提交 → 查询为 `COMPLETE` → `result` 可 `from_json` |
| TC-I-009 | 失败作业不可被误报成功 | REQ-004 | `FailingFlowRunner`：提交 → 查询恒为 `ERROR`，多轮查询结果一致 |
| TC-I-010 | 事件流水完整 | REQ-005 | 至少含「入队 / 开始 / 各 Task / 结束」，且正序 |
| TC-I-011 | `/healthz` | REQ-014 | 200 + `status=ok` |
| TC-I-012 | 错误体不泄露内部信息 | REQ-011 | 触发 500 的响应体不含 `Traceback`/`mysql`/密码字样 |
| TC-I-013 | Worker eager 模式跑通任务体 | REQ-002 | Celery `task_always_eager` 下 `kickoff_flow` 正确推进状态 |

### 2.4 系统层

| 编号 | 用例 | 守护 | 前置 |
|---|---|---|---|
| TC-S-001 | 真实 MySQL + Redis + Worker 全链路（`fake` 供应商） | REQ-001~006 | 本机 mysql/redis 已启动；`AMR_LLM_PROVIDER=fake` |
| TC-S-002 | 终态不可变（真实数据库并发写入） | REQ-004 | 作业终态后尝试写 `COMPLETE`，断言被拒且库中值不变 |
| TC-S-003 | 任意工作目录启动 | NFR-006 | 从 `/tmp` 启动 API，`/healthz` 可达 |
| TC-S-004 | 真实网关冒烟 | REQ-008 | `@pytest.mark.real_llm`，需 `OPENAI_*` 环境变量，**默认不跑** |
| TC-S-005 | 前端构建产物可用 | REQ-012 | `npm run build` 成功且 `dist/index.html` 存在 |

---

## 3. 需求追溯表

| REQ | 单元 | 契约 | 集成 | 系统 |
|---|---|---|---|---|
| REQ-001 提交作业 | TC-U-009 | — | TC-I-001, TC-I-002 | TC-S-001 |
| REQ-002 异步执行 | TC-U-015 | — | TC-I-013 | TC-S-001 |
| REQ-003 查询作业 | TC-U-014 | TC-C-001 | TC-I-004, TC-I-005 | TC-S-001 |
| REQ-004 状态机 | TC-U-001~004, 012, 013, 015 | TC-C-006~009 | TC-I-009 | TC-S-002 |
| REQ-005 事件流水 | TC-U-016 | TC-C-004 | TC-I-010 | TC-S-001 |
| REQ-006 结构化输出 | TC-U-007, 008, 018 | — | TC-I-008 | TC-S-001, TC-S-004 |
| REQ-007 双 Crew 编排 | TC-U-017, 019 | — | TC-I-008 | TC-S-001, TC-S-004 |
| REQ-008 多供应商 | TC-U-021, TC-U-024 | — | — | TC-S-004 |
| REQ-009 配置安全 | TC-U-020, TC-U-025 | — | TC-I-012 | TC-S-003 |
| REQ-010 输入校验 | TC-U-005, 006 | — | TC-I-003 | TC-S-001 |
| REQ-011 错误语义 | TC-U-010 | — | TC-I-006, 007, 012 | — |
| REQ-012 前端控制台 | — | — | — | TC-S-005 + 前端 vitest：`useJobPolling.spec.ts`（轮询间隔与停止规则）、`crew.spec.ts`（CT-001/002/005）、`result.spec.ts`（result 形状容错）、`url.spec.ts`（job_id 回填）、`App.spec.ts`（首屏与徽章） |
| REQ-013 可观测性 | — | — | TC-I-010 | TC-S-001 |
| REQ-014 健康检查 | — | — | TC-I-011 | TC-S-003 |
| NFR-001 离线可跑 | 全部（CI 在无密钥环境执行） | | | |
| NFR-004 分层依赖 | TC-U-022, 023 | | | |
| NFR-005 提交延迟 | — | — | TC-I-002 | — |
| NFR-006 任意工作目录 | — | — | — | TC-S-003 |

> 追溯表**随代码一起更新**：新增 REQ 或 TC 时同步改这张表（`000` §8 完成定义）。

---

## 4. 测试数据与 Fake

`tests/fakes/` 提供共享替身（**不是** mock 库的 `MagicMock`，而是行为明确的轻量实现）：

| Fake | 用途 | 特性 |
|---|---|---|
| `FrozenClock` | 时间注入 | `now()` 恒定，可手动推进 |
| `SeqIdGenerator` | ID 注入 | 返回 `job-1`、`job-2`…，断言可写死 |
| `MemoryJobRepository` | 单测/集成 | 生产级实现（WS-A 交付），非一次性 mock |
| `InlineTaskQueue` | 集成 | 同进程同步执行 |
| `FakeFlowRunner` | 单测/集成 | 返回固定 `FlowResult`，可配置抛异常 |
| `RecordingRepository` | 单测 | 记录写入序列，用于断言「ERROR 之后没有 COMPLETE」 |
| `FakeChatModel` | 单测 | 满足 `ChatModel`，固定输出 |

**原则**：Fake 与真实实现共享同一个 `JobRepository` 契约，因此 Fake 的行为不会「假装通过」——契约测试同时在两个实现上跑。

---

## 5. 反模式（review 会被打回）

- ❌ 断言里出现真实网络调用或 `sleep` 等待。
- ❌ 单元测试依赖真实 `.env` / 环境变量（必须用 `Settings` 显式构造或 `monkeypatch`）。
- ❌ 只测 happy path，错误路径没用例。
- ❌ 断言 `assert result is not None` 这种「不疼不痒」的写法，必须有具体值/形状断言。
- ❌ 为了让测试通过而放宽断言（例如把 `ERROR` 断言改成 `in ("ERROR","COMPLETE")`）。
- ❌ 一个测试里断言多个不相关的行为（失败时看不出哪儿坏了）。
