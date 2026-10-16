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
| 队列里积压吗 | `export REDISCLI_AUTH='<口令>'` 后 `redis-cli -n <REDIS_DB> LLEN celery` | 数字很大 → Worker 太慢或挂了 |
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

## 7. 升级、发布与回滚

### 7.1 依赖升级

依赖锁定在 `uv.lock`，升级用 `uv lock --upgrade-package <pkg> && uv sync --extra dev`，然后跑完整门禁。

### 7.2 回滚

应用无状态，回滚 = 切回上一个版本的代码再启动（数据库表结构向下兼容：只有新增列才会破坏，目前没有）。

发布用 `--no-ff`，所以一次发布就是一个合并提交，这是个能整体回退的单位。

> ⚠️ **回滚路径目前与不变量 2 冲突，待决策——下面这条是事实描述，不是推荐做法。**
> 在 `main` 上直接 `git revert -m 1 <merge-sha>` 会留下一个 `bugfix` 没有的提交，
> 不变量 2（`spec/000` §2.3）当场为红，而且没有合法办法消除它——消除就得把该提交弄回
> `development`，正是 §2.2 禁止的反向流动。守卫会拦住下一次发布：

```
$ python scripts/check_promotion.py development main --tag v0.2.0
阻断点：
  - main 上有 development 没有的提交（1 个），已经分叉——按 spec/000 §2.3 停下来查因
```

> 怎么处置**尚未决定**：给不变量 2 开例外，还是让回滚另走一条不产生 `main` 私有提交的
> 路径。这件事与 `spec/000` §2.6 的 hotfix 回流是同一个取舍，两者应当一起定。

### 7.3 发布清单（`spec/000` §2.5）

发布路径：`bugfix` → CI 自动 PR → Codex 以 `--ff-only` 合进 `development` → **人**以 `--no-ff` 合进 `main` → 打 tag。
逐条走，缺一条就停在原地：

1. 先跑守卫。它**只读**：给结论、阻断点和该执行的命令，不会替你合并：

   ```bash
   git fetch --prune origin
   python scripts/check_promotion.py development main --tag v0.1.0
   ```

2. 检查清单：

   - [ ] 守卫放行（`main` 上没有 `bugfix` 之外的提交；版本号没被用过）
   - [ ] 系统测试与冒烟在真实中间件上跑过：`make test-system && bash scripts/smoke.sh`
   - [ ] `uv.lock` 没有未解释的变动；`config.py` 的字段与 `.env.example` 一致
   - [ ] 发布说明写好（面向使用者的改动都在 `docs/` 里）

3. 合并与打 tag（**人**执行；`--no-ff` 是刻意的，见 `spec/000` §2.2）：

   ```bash
   git checkout main
   git merge --no-ff origin/development -m "release: v0.1.0"
   git tag -a v0.1.0 -m "v0.1.0"
   git push origin main --tags
   ```

4. 发布后确认不变量没被破坏：

   ```bash
   python scripts/check_promotion.py bugfix development
   ```

### 7.4 hotfix

**暂不支持**独立通道（`spec/000` §2.6）：`main` 上的紧急修复同样走
`bugfix` → `development` → `main` 两次提升。真需要独立通道时，按 §2.6 的顺序引入，
否则 `main` 上会留下 `bugfix` 没有的提交，下一次发布就会从「提升」变成「真合并」。

---

## 8. 自动评审（Codex QA）

`.github/workflows/codex-qa.yml`：`ci.yml` 跑完之后接手，对刚推上 `bugfix` 的 commit 做
code review，把 Findings 贴回 PR（`spec/000` §2.9 的 review 循环）。

**它不做什么**（刻意的）：不合并、不推送、不改代码。`permissions.contents` 是 `read`，
合并 `development` 是 QA 的决定，需要人明确授权之后才加——`tests/unit/test_ci_workflow.py`
里有一条断言专门挡着「顺手把 contents 提成 write」。

### 8.1 一次性配置

1. 仓库 **Settings → Secrets and variables → Actions → New repository secret**，
   建 `AMR_CODEX_KEY`，值是模型端点的 API key。
2. 确认 workflow 里 `[model_providers.amr]` 的 `base_url` 与 `model` 对得上你要用的端点
   （默认 `https://api.deepseek.com/` + `deepseek-flash`）。换端点改这一段即可。
3. **workflow 文件必须存在于默认分支上才会生效**。`workflow_run` 与 `issue_comment`
   这类事件取的是**默认分支**的 workflow 文件——所以本文件要先经
   `bugfix` → `development` → `main` 走到 `main` 之后才会真正开跑。在那之前它**不会有
   任何运行记录**，check 也全绿，容易误判成「配好了」。

### 8.2 为什么不监听那句 `@codex` 评论

`ci.yml` 的 `open-pr` job 会发一句 `@codex …`。它**不能**用来触发这个 workflow：

> GitHub 明确规定，**用 `GITHUB_TOKEN` 执行的动作所产生的事件，不会启动新的
> workflow run**（防止递归触发）。

那句评论正是 `GITHUB_TOKEN` 发的，所以监听 `issue_comment` 等于监听一个永不触发的事件：
运行记录一片空白、check 全绿，唯一症状是「Codex 从不出现」。本 workflow 因此改用
`workflow_run`（监听「CI 跑完了」），不受那条规则限制。

同一条规则在本仓库已经咬过两次，排查时先想到它：

| 症状 | 成因 |
|---|---|
| CI 自己开的 PR，check 列表里某个 job 永远 `skipping` | 该 job 只挂 `pull_request`，而 bot 开的 PR 其运行被扣在 `action_required` |
| 换触发方式后 workflow 完全不跑 | 事件由 `GITHUB_TOKEN` 产生，不启动新运行 |
| `workflow_run` 的 workflow 明明写了却不跑 | 文件还不在默认分支（`main`）上 |

### 8.3 排查

| 症状 | 看哪里 |
|---|---|
| PR 上出现「Codex 没有产出结论」 | 该次运行的 Actions 日志；多半是 `AMR_CODEX_KEY` 失效/额度用尽，或端点不认配置里的模型名 |
| 完全没有评论 | 有没有打开的 `bugfix → development` PR；以及 §8.1 第 3 条 |
| 评审结论明显跑偏 | workflow 里那段 `codex exec` 的提示词——分层红线与分级口径都写在那儿 |
| token 烧太快 | `timeout-minutes`、`concurrency` 分组，以及提示词里要求的输出篇幅 |
