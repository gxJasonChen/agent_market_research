# 测试计划

规格依据：[`spec/040-test-strategy.md`](../spec/040-test-strategy.md)（用例矩阵与需求追溯表在那里，本文件讲**怎么执行**）。
需求依据：[`spec/001-requirements.md`](../spec/001-requirements.md)。

---

## 1. 分层与执行方式

| 层 | 命令 | 外部依赖 | 典型耗时 | 何时跑 |
|---|---|---|---|---|
| 单元 | `make test-fast` | 无 | < 2s | 每次改代码 |
| 单元 + 契约 + 集成 | `make test` | 契约层要 MySQL（连不上自动 skip） | < 1min | **提交前门禁** |
| 覆盖率 | `make coverage` | 同上 | < 1min | 提交前门禁 / CI |
| 系统 | `make test-system` | 真实 MySQL + Redis + Worker | 1–5min | Codex review 通过后 |
| 系统（真实模型） | `make test-system -- --run-real-llm` | 额外要模型网关密钥 | 5–20min | 发布前 |

```bash
# 本地门禁（与 CI 完全一致）
make lint && make type && make test && make coverage

# 只跑某条用例
.venv/bin/python -m pytest tests/unit/test_status.py -q -k truth_table
```

---

## 2. 离线红线

`make test`（单元 + 契约 + 集成）**必须**能在无网络、无密钥、无 MySQL/Redis 的机器上全绿。
做不到这条，TDD 的红绿循环就没法在本地快速转起来，CI 也会变成看天吃饭。

实现方式：
- 全部外部依赖走 `spec/021` 的端口，测试注入内存替身（`tests/fakes/`）
- 契约层的 MySQL 用例在连不上时 `pytest.skip`，不制造假绿（skip 是显式的，不是通过）
- 真实模型调用只在 `--run-real-llm` 下发生

自检：

```bash
# 断网 + 无密钥仍应全绿（MySQL 用例显示为 skipped）
env -u OPENAI_API_KEY -u OPENAI_API_BASE AMR_LLM_PROVIDER=fake make test
```

---

## 3. 系统测试怎么跑

### 3.1 前置

```bash
sudo service mysql start && sudo service redis-server start
cp .env.example .env && $EDITOR .env     # MySQL 口令、模型网关（fake 路径不需要网关）
```

系统测试用**独立测试库**（`<MYSQL_DATABASE>_test`）与**独立 Redis 库**，
并在端口 8112 起自己的 API/Worker，不会打到开发中的服务上。

### 3.2 执行

```bash
make test-system
```

它会真的做这些事：

1. 探测 MySQL / Redis 端口，不可达直接 skip（并说明原因）
2. 以 `AMR_LLM_PROVIDER=fake` 起 Celery Worker 与 uvicorn（日志在 `.artifacts/system-*.log`）
3. 等 Worker 通过 `celery_app.control.ping()` 应答、等 `/healthz` 返回 200
4. 跑用例，结束后回收两个子进程

### 3.3 用例清单

| 编号 | 文件 | 验证什么 |
|---|---|---|
| TC-S-001 | `test_e2e_fake_llm.py` | 提交 → Worker 执行 → 结构化结果落库；并用独立连接交叉验证库里状态 |
| TC-S-002 | `test_terminal_state_immutability.py` | 真实 MySQL 上「失败作业不可被改写成成功」；并发收尾只有一个成功 |
| TC-S-003 | `test_e2e_fake_llm.py` | 从 `/tmp`、`/` 启动 API 仍能读到配置 |
| TC-S-004 | `test_e2e_real_llm.py` | 真实网关跑通全链路（**默认跳过**） |
| TC-S-005 | `test_frontend_build.py` | 前端 type-check + 生产构建 + 接口路径与 spec/022 一致 |

### 3.4 排障

| 症状 | 先看 |
|---|---|
| 作业一直 `PENDING` | `.artifacts/system-worker.log`——多半是 Worker 没连上 broker |
| `等待 Celery Worker 就绪超时` | Worker 是否 import 失败（缺依赖 / PYTHONPATH 不对） |
| 作业变 `ERROR` | `GET /api/crew/{id}` 的 `result`（错误摘要）与 `events` 流水 |
| 前端用例 skip | `npm` 不在 PATH，或 `src/web` 尚未交付 |

---

## 4. 覆盖率要求

| 范围 | 下限 | 说明 |
|---|---|---|
| `src/amr/domain/` | ≥ 95% | 状态机与 Schema 是正确性核心，不能有盲区 |
| `src/amr/services/` | ≥ 95% | 状态迁移的唯一写入口 |
| 整体 `src/amr/` | ≥ 85% | 适配层与装配点允许少量未覆盖 |

```bash
make coverage            # 终端报告 + coverage.xml
```

---

## 5. 需求追溯

每条 `REQ-xxx` 由哪些用例守护，维护在
[`spec/040-test-strategy.md §3`](../spec/040-test-strategy.md) 的追溯表里。
新增需求或新增用例时**同步更新那张表**（`spec/000-constitution.md` §8 完成定义）。

抽查方式：

```bash
# 列出所有带追溯号注释的用例
grep -rn "REQ-" tests/ --include=*.py | grep '"""'
```

---

## 6. 多 Agent 流水线中的位置

```
开发者本地：make lint / type / test / coverage   ← 提交前门禁，红则绝不提交
        ↓ push bugfix
CI(gate job)：在干净环境重跑同样的门禁（含 MySQL service）
        ↓
CI(open-pr job)：自动开 PR 到 development，通知 Codex
        ↓
Codex：code review + make test-system（必要时 --run-real-llm）
        ↓ PASS
Codex：--ff-only 合并 development → 跑 python scripts/check_promotion.py 自检
        ↓
人：--no-ff 合并 main → 打 tag（发布清单见 docs/RUNBOOK.md §7）
```
