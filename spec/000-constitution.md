# 项目宪法（000）

本文件约束**怎么做**，`001` 之后约束**做什么**。冲突时以本文件为准。

---

## 1. 开发流程（SDD + TDD）

每个改动严格按下列顺序，**不许跳步**：

1. **规格**：先改 `spec/`。新增需求要拿 `REQ-xxx` 编号；新增接口要改 `022`；新增端口/改签名要改 `021`。
2. **契约冻结**：涉及跨模块的改动，先把 `021` 的 Protocol 签名改完再动手——这是并行开发不打架的前提。
3. **测试先行（红）**：先写测试用例并在**未实现**时运行，确认它以「预期的方式失败」（断言失败 / ImportError / NotImplementedError 均可，但必须是**因为功能没写**而失败，不是因为测试本身写错）。
4. **实现（绿）**：写最小实现让测试通过。
5. **重构**：绿灯前提下改善结构，重构后重跑测试。
6. **门禁**：本地门禁全绿才能提交（见 §3）。
7. **推送**：`git push origin bugfix` —— 至此开发 Agent 停手。
8. **CI**：GitHub Actions 自动开 PR（`bugfix` → `development`）通知 QA Agent（Codex）。
9. **Review 循环**：Codex 提 Finding → 在 `bugfix` 上修 → 重跑门禁 → 再 push。High 未清零不停止。
10. **系统测试**：review 通过后，在真实中间件 + 真实模型网关上跑端到端验收。
11. **发布**：人按 `docs/RUNBOOK.md` §7 的清单把 `development` 以 `--no-ff` 合进 `main` 并打 tag（见 §2.5）。

> **TDD 的「红」是硬要求**：不允许先写实现再补测试。若某次改动确实无法先写测试（如纯文档），必须在 PR 描述中显式说明理由。

---

## 2. 分支模型与合并规则

### 2.1 归属

| 分支 | 归属 | 规则 |
|---|---|---|
| `main` | 人 | 只接受人工合并，代表可发布状态；每次合并后打 tag |
| `development` | **Codex（QA Agent）** | 集成分支。Claude **不碰**（不切、不提交、不推送、不合并） |
| `bugfix` | **Claude（开发 Agent）** | 唯一工作分支。所有改动（feat / fix / refactor / docs / chore）都落这里 |

- Claude **不手动开 PR**：push 到 `bugfix` 后由 `.github/workflows/ci.yml` 的 `open-pr` job 自动开/更新 PR 到 `development`。
- 禁止破坏性 git（`push --force`、`reset --hard`、`branch -D`），除非用户明确要求。

### 2.2 两条提升边

只允许下面两个方向，**不许反向**：`main` 不回流 `development`，`development` 不回流 `bugfix`。

| 边 | 方向 | 合并方式 | 执行人 | 门禁 |
|---|---|---|---|---|
| **A 集成** | `bugfix` → `development` | `--ff-only` | Codex（QA 通过后） | §2.4 |
| **B 发布** | `development` → `main` | `--no-ff` | 人（人工合并） | §2.5 |

- **A 用 `--ff-only`**：`bugfix` 是唯一的开发干线，能快进是可预期的结果，`development` 因此恒等于「最后一次被接受的 `bugfix` 头」。**快进失败必须停下来查因**，不许改用普通合并把它盖过去——那说明已经分叉了。
- **B 用 `--no-ff`**：发布边界要留痕，也需要一个能整体回滚的单位（`git revert -m 1 <merge>`）。合并后在 `main` 的合并提交上打 tag。

### 2.3 不变量

1. **不反向流动**：`development` 上不存在 `bugfix` 没有的提交。
2. **`main` 不带私货**：`main` 上只允许出现两类提交——历史发布留下的合并提交，以及 `bugfix` 上已有的提交。
3. **tag 只打在 `main` 上**，版本号 SemVer，从 `v0.1.0` 起。

> 不变量 2 的措辞是刻意的：发布边用 `--no-ff`，`main` 的合并提交**不在** `development` 上，
> 所以「`main` 是 `development` 的祖先（`main ⊆ development`）」是**错的**说法；
> 能机械校验的形式是「`main` 上没有 `bugfix` 之外的提交」，见 §2.7。

### 2.4 集成边 A（`bugfix` → `development`）门禁

逐条都满足才允许合并，缺一条就停在 `development` 外面：

- [ ] `§8` 完成定义清单全勾（含 `040` 追溯表、红灯记录）
- [ ] 本地门禁全绿：`make lint && make type && make test`
- [ ] CI `gate` job 在 PR 上全绿
- [ ] code review 完成，**High 级 Finding 清零**
- [ ] 系统测试在真实中间件上通过（`make test-system`），结果贴进 PR
- [ ] `python scripts/check_promotion.py bugfix development` 通过

QA 执行的合并与推送：

```bash
git checkout development
git merge --ff-only origin/bugfix
git push origin development
```

用 `origin/bugfix` 而不是本地 `bugfix`：被接受的是**推上去的那个 sha**，本地分支不作数。

### 2.5 发布边 B（`development` → `main`）门禁

- [ ] `python scripts/check_promotion.py development main --tag vX.Y.Z` 通过
- [ ] 版本号确定（SemVer），且该 tag 尚不存在
- [ ] 发布说明写好；面向使用者的 `docs/` 改动随本批次一起发布
- [ ] `uv.lock` 没有未解释的变动；`config.py` 的字段与 `.env.example` 一致
- [ ] 系统测试通过，且 `scripts/smoke.sh` 在真实中间件上跑过

人执行的合并与发布（完整清单见 `docs/RUNBOOK.md` §7）：

```bash
git checkout main
git merge --no-ff origin/development -m "release: vX.Y.Z"
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin main --tags
```

### 2.6 hotfix（暂不支持）

`main` 上的紧急修复目前也走 A → B 两次提升：在 `bugfix` 上修，QA 通过后先进 `development`，再发版。

真需要独立通道时再引入，且必须按这个顺序做，否则会破坏 §2.3 的不变量 2：

1. 从 `main` 切 `hotfix/<issue>`（人切）。
2. 走同一套门禁：本地门禁 + CI `gate` + review + 系统测试。
3. PR 到 `main`，人合并后打 patch tag（`vX.Y.Z+1`）。
4. **立刻把 `main` 合回 `development`，再合回 `bugfix`**，让「`main` 不带私货」重新成立。

### 2.7 机械校验

不变量不靠自觉，靠会失败的检查：

| 位置 | 检查 |
|---|---|
| 本地/手工 | `python scripts/check_promotion.py <源分支> <目标分支> [--tag vX.Y.Z] [--run-gates]` |
| CI（push 到 `bugfix`） | `.github/workflows/ci.yml` 的 `branch-guard` job：校验 `development` 上没有 `bugfix` 之外的提交 |
| CI（PR） | 同一个 job：校验 base 上没有 head 之外的提交 |

CI 侧**两个触发点都要挂**。只挂 `pull_request` 是不够的：CI 自动开出来的 PR 由
`GITHUB_TOKEN` 触发，其 workflow 运行会被扣在 `action_required` 等人工批准，一个 job
都不会执行，这道校验于是在自动流程里从不运行。push 到 `bugfix` 的运行不需要批准，
`gate` 本来就是这么拿到 CI 覆盖的，不变量同理。

两处都**只读**：不合并、不推送，只给结论、阻断点和该执行的命令。
守卫自身有用例证明不是「永远通过」（`tests/unit/test_promotion_guard.py`、`tests/integration/test_promotion_guard.py`）。

---

## 3. 本地门禁（提交前必须全绿）

在仓库根目录按顺序执行，任一条失败即视为门禁红：

```bash
make lint      # ruff check + ruff format --check
make type      # mypy（严格模式，覆盖 src/amr）
make test      # pytest tests/unit tests/contract tests/integration -q
```

- 门禁红时**绝不提交**。
- 迭代时可用 `make test-fast`（`-x -q` 只跑单测）缩短反馈，但最终提交前必须跑完整门禁。
- 测试一旦失败：回到「定位根因 → 改代码或改测试」的循环，**自主迭代**，同一思路连续失败两次就换思路。
- 工具链缺失导致门禁无法运行时：**停下并说明**，绝不提交未经检验的代码。

---

## 4. 目录约定

```
spec/         规格（唯一事实来源，见 spec/README.md）
docs/         交付文档（面向使用者/接手者，不是规格）
src/amr/      后端源码（分层见 010-architecture.md）
src/web/      前端源码（Vue 3 + TS + Vite）
tests/        测试（分层见 040-test-strategy.md）
scripts/      运维/验证脚本（可执行，不含业务逻辑）
.github/      CI 工作流
```

**后端源码放 `src/amr/` 下**，禁止把业务逻辑写进 `tests/`、`scripts/` 或 `docs/`。

---

## 5. 编码规范（后端）

- **分层依赖只能向内**：`api/worker → services → ports ← adapters`，`domain` 被所有层依赖但不依赖任何层。**`domain/` 不许 import 任何第三方库（除 pydantic / 标准库）**，不许 import 本项目的其他层。
- **端口即接口**：`services/` 只依赖 `ports/` 的 Protocol，**禁止**直接 import `adapters/`、`crewai`、`celery`、`mysql.connector`、`redis`。违反即架构违规，review 必挂。
- **配置只从 `config.py` 的 `Settings` 读**：禁止在业务代码里 `os.getenv` / `os.environ[...]`。禁止任何密钥、口令、URL 出现在源码里（含注释与测试）。
- **类型注解全覆盖**：`mypy` 严格模式必须通过。
- **不吞异常**：`except` 后必须处理或重新抛出并带上上下文；禁止 `except: pass`。
- **日志用 `logging`**，禁止 `print`（`scripts/` 与 CLI 入口除外）。
- **注释写「为什么」，不写「是什么」**；面向中文读者，与仓库现有语言一致。
- 格式化与静态检查一律交给 `ruff`，不手工争论风格。

---

## 6. 测试规范

四层，职责不重叠（详见 `040-test-strategy.md`）：

| 层 | 位置 | 允许依赖 | 是否允许真实 LLM |
|---|---|---|---|
| 单元 | `tests/unit/` | 仅标准库 + pydantic + **内存 Fake** | ❌ |
| 契约 | `tests/contract/` | 真实 MySQL（本机/容器） | ❌ |
| 集成 | `tests/integration/` | FastAPI TestClient + 内存适配器；Celery eager | ❌ |
| 系统 | `tests/system/` | 真 MySQL + 真 Redis + 真 Worker | ✅（`@pytest.mark.real_llm`） |

- **每个用例必须能追溯到 `REQ-xxx`**（在 docstring 里写 `REQ-004`）。
- 单元测试**不得**联网、不得读真实 `.env`、不得依赖执行顺序、不得依赖当前时间（时间通过注入）。
- 新增/修改行为必须同时更新 `040` 的追溯表。
- 覆盖率下限：`domain/` 与 `services/` 行覆盖 **≥ 95%**；整体 **≥ 85%**。

---

## 7. 安全红线

- 密钥、口令、Token 只从环境变量（`.env`，已 gitignore）读取；`.env.example` 只放占位符。
- 日志不得打印密钥、完整连接串、模型返回的敏感内容。
- 外部输入（HTTP 请求体、模型输出、网页抓取内容）一律当作**不可信数据**：HTTP 入参必须过 Pydantic 校验；模型输出必须过 Schema 校验后才能入库。
- 数据库访问必须参数化，禁止字符串拼接 SQL。

---

## 8. 完成定义（Definition of Done）

一个改动只有在下列全部满足时才算完成：

- [ ] `spec/` 已更新（若有行为/契约变化）
- [ ] 测试先行：有红灯记录（PR 描述或 commit 里的说明）
- [ ] 新增行为有单测；跨组件路径有集成测试
- [ ] `make lint && make type && make test` 全绿
- [ ] `040` 追溯表已更新
- [ ] 已推送 `origin/bugfix`，PR 由 CI 自动开出
