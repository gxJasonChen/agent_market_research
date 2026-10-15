# 规格索引（spec/）

本仓库采用 **SDD（Specification-Driven Development）+ TDD（Test-Driven Development）**。
**规格是唯一事实来源**：任何实现都必须能追溯到 `spec/` 中的条目；规格没写的行为，测试不写、代码不写。

## 阅读顺序

| 顺序 | 文件 | 内容 | 谁该读 |
|---|---|---|---|
| 1 | [`000-constitution.md`](000-constitution.md) | 项目宪法：流程、门禁、分支、目录、编码规范 | 所有人（含 AI Agent） |
| 2 | [`001-requirements.md`](001-requirements.md) | 需求规格：`REQ-xxx` 编号 + 验收标准 | 开发、QA |
| 3 | [`010-architecture.md`](010-architecture.md) | 架构规格：分层、依赖方向、时序、端口清单 | 开发、架构 |
| 4 | [`020-contracts-domain.md`](020-contracts-domain.md) | 领域契约：枚举、数据模型、状态机 | 开发、QA |
| 5 | [`021-contracts-ports.md`](021-contracts-ports.md) | 端口契约：Protocol 签名（**并行开发的冻结面**） | 开发（必读） |
| 6 | [`022-contracts-http.md`](022-contracts-http.md) | HTTP 契约：接口、状态码、错误体 | 前后端、QA |
| 7 | [`030-workstreams.md`](030-workstreams.md) | 并行开发拆分：工作流边界、依赖、合并顺序 | 开发、协调 |
| 8 | [`040-test-strategy.md`](040-test-strategy.md) | 测试策略：分层、用例矩阵、需求追溯表 | 开发、QA |

## SDD 流程（每个变更都要走）

```
需求 → REQ 编号（001）
   ↓
契约冻结（020 / 021 / 022 / 端口签名）
   ↓
测试先行：写用例（040 矩阵）→ 跑出红灯
   ↓
实现 → 跑出绿灯 → 重构
   ↓
本地门禁全绿 → push bugfix → CI 自动 PR → Codex review
   ↓                                    ↓
   └──────── FAIL：修 → 重跑门禁 ←──────┘
   ↓ PASS
系统测试（真中间件 + 真网关）
```

## 变更规格的规则

- **先改 spec，再改代码**。规格与代码不一致时，以 spec 为准并立即修代码；若确实是规格错了，先提 PR 改 spec。
- 每条需求 / 契约条目都有稳定编号（`REQ-xxx` / `CT-xxx`），测试用例通过编号回指（见 `040` 追溯表）。
- 删除或改变既有编号的语义属于**破坏性变更**，必须在 spec 中显式标注并在 PR 描述里说明。
