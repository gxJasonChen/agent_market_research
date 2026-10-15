# 运行手册（RUNBOOK）

面向「要把它跑起来 / 跑挂了要知道先看哪里」的人。
架构与设计理由见 [`TECHNICAL.md`](TECHNICAL.md)，接口契约见 [`spec/022-contracts-http.md`](../spec/022-contracts-http.md)。

---

## 1. 组件与端口

| 组件 | 默认端口 | 作用 | 挂了会怎样 |
|---|---|---|---|
| FastAPI | 8012 | 受理与查询作业 | 提交/查询都不可用 |
| Celery Worker | —（消费 Redis 队列） | 真正执行 Flow | 作业停在 `PENDING` 不动 |
| MySQL | 3306 | `jobs` / `events` 两张表 | 提交返回 503；查询返回 503 |
| Redis | 6379（本系统用 `REDIS_DB` 指定的库） | Celery broker | 提交返回 503，作业被置 `ERROR` |
| 前端（Vite dev） | 5173 | 控制台 | 不影响 API |

---

## 2. 启停

### 2.1 一键（推荐）

```bash
scripts/dev_up.sh          # 拉起 Worker + API，日志进 .artifacts/
scripts/dev_up.sh status   # 看进程与 /healthz
scripts/dev_up.sh stop
```

### 2.2 手工

```bash
# 终端 A —— Worker（真正跑推理的进程）
make run-worker

# 终端 B —— API
make run-api

# 终端 C —— 前端（可选）
cd src/web && npm run dev
```

### 2.3 停止

```bash
scripts/dev_up.sh stop
# 或按进程名
pkill -f 'celery -A amr.worker.tasks'
pkill -f 'uvicorn amr.api.app'
```

> 清空待处理队列（**会丢作业，谨慎**）：
> ```bash
> export REDISCLI_AUTH="$(grep '^REDIS_PASSWORD' .env | cut -d= -f2- | tr -d ' ')"
> redis-cli -n <REDIS_DB> FLUSHDB
> unset REDISCLI_AUTH
> ```
> **不要**用 `redis-cli -a '<口令>'` —— 那会把口令写进 shell 历史与 `ps` 输出。

---

## 3. 健康检查与冒烟

```bash
curl -s http://127.0.0.1:8012/healthz
# {"status":"ok","version":"0.1.0"}

scripts/smoke.sh
# 提交一次作业 → 轮询到终态 → 校验 result 四个字段与事件流水
```

`/healthz` **只反映进程存活**，不探测 MySQL/Redis（避免探活把负载打到下游）。
要确认依赖可用，跑 `scripts/smoke.sh` 或看下面 §5。

---

## 4. 配置

全部配置项与说明见 [`.env.example`](../.env.example)。几条硬规矩：

- **配置只从环境变量读**，源码里没有任何密钥（`REQ-009`）。
- `.env` 用**绝对路径**定位，从任意工作目录启动都读得到（`NFR-006`）。因此**不需要** `cd` 到源码目录再启动。
- Redis 口令含 `@` `:` `/` 等特殊字符时**照原样填**，代码会 `quote_plus` 编码，不要手工转义。

常改的几项：

| 变量 | 说明 |
|---|---|
| `AMR_LLM_PROVIDER` | `openai` / `oneapi` / `ollama` / `fake` |
| `AMR_API_PORT` | API 端口，默认 8012 |
| `AMR_CORS_ORIGINS` | 逗号分隔，默认只放行本地 5173（**不是** `*`） |
| `REDIS_DB` | 与同机其他项目隔离队列时改这里 |
| `AMR_LOG_LEVEL` | 日志级别 |

---

## 5. 故障处理

### 5.1 作业一直 `PENDING`

**含义**：作业已落库，但没人取走执行。

| 排查 | 命令 | 结论 |
|---|---|---|
| Worker 在跑吗 | `pgrep -af 'celery -A amr.worker.tasks'` | 没有 → 启动它 |
| Worker 连上 broker 了吗 | `cd src && ../.venv/bin/python -m celery -A amr.worker.tasks:celery_app inspect ping` | 无响应 → 看 Worker 日志 |
| 队列里积压吗 | `REDISCLI_AUTH='<口令>' redis-cli -n <REDIS_DB> LLEN celery` | 数字很大 → Worker 太慢或挂了 |
| Worker 启动就退 | `.artifacts/worker.log` 的 traceback | 多半是依赖缺失或配置读不到 |

### 5.2 作业变 `ERROR`

直接读 `result` 与 `events`：`result` 是**经过筛选的摘要**（本系统自己写的文案 +
Schema 校验失败的字段名），`events` 是逐步产出。

> 外来异常的**原文不进 `result`**（可能带网关 URL、请求参数甚至密钥），
> 全文在服务端日志里——排障时两者对着看。

查询：

```bash
curl -s http://127.0.0.1:8012/api/crew/<job_id> | python3 -m json.tool
```

常见原因：

| `result` 里出现 | 原因 | 处理 |
|---|---|---|
| `Schema` 字样 | 模型输出不符合 Schema（字段缺失/类型错） | 看错误里点名的 `strategy.tactics` 之类字段；必要时收紧/放宽 Prompt |
| `FlowExecutionError` + 工具名 | 检索工具不可用（Serper 配额/密钥） | 检查 `SERPER_API_KEY` 与配额 |
| `RuntimeError（详情见服务端日志）` 之类 | 外来异常，原文在日志里 | 查 Worker 日志里对应的 job_id |
| 模型网关超时/鉴权 | 网关地址或密钥不对 | 检查 `OPENAI_*` 配置 |
| `任务队列不可用` | 提交时 broker 不可达 | 起 Redis；作业已被置 ERROR，需重新提交 |

> **注意**：作业一旦进入 `COMPLETE`/`ERROR` 就**不可改写**（`REQ-004`）。
> 这是刻意设计——源项目那个「失败作业被误报成功」的严重缺陷就是这么来的。
> 重跑请**重新提交**，不要试图改历史作业的状态。

### 5.3 作业卡在 `RUNNING` 不动

**先跑回收脚本看看**（默认 dry-run，只打印不改）：

```bash
.venv/bin/python scripts/reap_stuck_jobs.py --older-than 60          # 看有哪些
.venv/bin/python scripts/reap_stuck_jobs.py --older-than 60 --apply  # 确认后回收
```

脚本会把超时仍非终态的作业标成 `ERROR` 并写明原因。

**为什么会卡住**：`PENDING` 侧有「入队失败即置 ERROR」的防护，`RUNNING` 侧没有——
执行器对**暂时性**存储故障会重试收尾写入（`services/orchestrator.py::_finalize`，默认 3 次），
重试耗尽后（比如数据库长时间不可用）只能靠这个脚本兜底。

**为什么重试「写入」而不重跑整条 Flow**：结果已经在手里，重试写入是毫秒级动作；
重跑一遍要再烧一遍 token，两者代价差几个数量级。

相关配置（`src/amr/worker/tasks.py`）：

| 配置 | 值 | 作用 |
|---|---|---|
| `broker_transport_options.visibility_timeout` | 6 小时 | 小于它，长 Flow 会被误判为「消息丢了」而重投 |
| `task_soft_time_limit` / `task_time_limit` | 45 / 50 分钟 | Flow 挂死时把 Worker 槽位收回来 |
| `kickoff_flow` 的 `max_retries` | 3（间隔 30s） | 只针对存储**暂时**不可用；不重跑整条 Flow |

### 5.4 提交返回 503

| detail | 原因 | 处理 |
|---|---|---|
| `任务队列不可用` | Redis 不可达 | 起 Redis，检查 `REDIS_*` 与口令特殊字符 |
| `存储不可用` | MySQL 不可达 | 起 MySQL，检查 `MYSQL_*`；确认库已建 |

### 5.5 提交返回 422

入参不合规。规则见 [`spec/020 §3`](../spec/020-contracts-domain.md)：
`customer_domain` 3–255 字符、`project_description` 10–4000 字符，两者都会先去掉首尾空白再判长度。

### 5.6 前端连不上后端

- 开发环境走 Vite proxy（`/api` → `127.0.0.1:8012`），**不需要**改后端 CORS；
- 直连时设 `VITE_API_BASE`，并把来源加进 `AMR_CORS_ORIGINS`。

---

## 6. 数据库

两张表由 `MySQLJobRepository` 在首次使用时自动建（`IF NOT EXISTS`），无需手工 DDL。

```sql
-- 作业表
SELECT job_id, status, updated_at FROM jobs ORDER BY created_at DESC LIMIT 20;

-- 某个作业的完整流水
SELECT timestamp, data FROM events WHERE job_id = '<id>' ORDER BY id;

-- 卡住的作业（长期非终态）
SELECT job_id, status, created_at FROM jobs
WHERE status IN ('PENDING','RUNNING') AND created_at < NOW() - INTERVAL 30 MINUTE;
```

**清理**（谨慎，会级联删事件）：

```sql
DELETE FROM jobs WHERE job_id = '<id>';   -- events 有 ON DELETE CASCADE
```

---

## 7. 升级与回滚

- 依赖锁定在 `uv.lock`，升级用 `uv lock --upgrade-package <pkg> && uv sync --extra dev`，然后跑完整门禁。
- 应用无状态，回滚 = 切回上一个版本的代码再启动（数据库表结构向下兼容：只有新增列才会破坏，目前没有）。
- 发布路径：`bugfix` → CI 自动 PR → Codex 合并 `development` → 人工 merge `main` → 打 tag。
