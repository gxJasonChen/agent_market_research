# HTTP 契约（022）

前端与后端、Claude 与 Codex 之间的**冻结接口**。改这里等于改对外契约，必须先改本文件。

- 基址：`http://<host>:8012`
- 交互式文档：`/docs`（FastAPI 自动生成，须与本文件一致）
- 内容类型：`application/json; charset=utf-8`
- 时间格式：ISO-8601 带时区偏移，如 `2026-10-15T10:20:30+08:00`

---

## CT-001 `POST /api/crew` —— 提交作业

**请求体**

| 字段 | 类型 | 必填 | 约束（`REQ-010`） |
|---|---|---|---|
| `customer_domain` | string | 是 | strip 后长度 3–255 |
| `project_description` | string | 是 | strip 后长度 10–4000 |

```json
{
  "customer_domain": "openai.com",
  "project_description": "为一家 AI 基础设施公司制定新品发布营销方案"
}
```

**响应**

| 状态码 | 场景 | 响应体 |
|---|---|---|
| `202` | 受理成功 | `{"job_id": "<uuid4>"}` |
| `422` | 入参非法 | `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`（FastAPI 默认形状） |
| `503` | 队列或存储不可用 | `{"detail": "任务队列不可用"}` |
| `500` | 未预期错误 | `{"detail": "内部错误"}`（**不含堆栈与连接串**） |

**语义保证**

1. 返回 `202` 时，作业已以 `PENDING` 落库**且**已成功入队（先落库后入队的顺序不可颠倒）。
2. 入队失败时，作业被置为 `ERROR` 并返回 `503`，**不留下僵尸 `PENDING`**（`REQ-011`）。
3. 接口不等待推理（`REQ-002`），P95 `< 200ms`（`NFR-005`）。

**curl**

```bash
curl -s -X POST http://127.0.0.1:8012/api/crew \
  -H 'Content-Type: application/json' \
  -d '{"customer_domain":"openai.com","project_description":"为一家 AI 基础设施公司制定新品发布营销方案"}'
# {"job_id":"7f3c...-..."}
```

---

## CT-002 `GET /api/crew/{job_id}` —— 查询作业

**路径参数**：`job_id`（string，必填）

**响应**

| 状态码 | 场景 |
|---|---|
| `200` | 作业存在 |
| `404` | 作业不存在 → `{"detail": "job not found"}` |
| `503` | 存储不可用 |

**响应体**

```json
{
  "job_id": "7f3c...",
  "status": "PENDING | RUNNING | COMPLETE | ERROR",
  "result": null,
  "events": [
    { "timestamp": "2026-10-15T10:20:30+08:00", "data": "Flow Started" }
  ]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `job_id` | string | 回显 |
| `status` | string | 见 `020` §1.1；**新增状态不视为破坏性变更**（前端须按字符串容错） |
| `result` | object \| null \| string | `COMPLETE` → 结构化对象（形状见 `020` §2.3）；`ERROR` → 错误摘要字符串；`PENDING`/`RUNNING` → `null` |
| `events` | array | 按 `timestamp` **正序**（最早在前）；每项 `{timestamp, data}` |

**语义保证**

1. `result` 只在 `COMPLETE` 时是结构化对象；后端**不得**把裸文本塞进 `result` 当结构化结果（`REQ-006`）。
2. 作业进入终态后 `status` 与 `result` **永不再变**（`REQ-004`）。
3. 源项目的 `result` 为 `json.loads(str(...))` 后的自由文本；本系统返回受 Schema 约束的对象（`REQ-006`，**刻意差异**）。

**curl**

```bash
curl -s http://127.0.0.1:8012/api/crew/<job_id> | jq
```

---

## CT-003 `GET /healthz` —— 健康检查

供 CI、系统测试与运维探活使用（`REQ-014`）。

**响应 `200`**

```json
{ "status": "ok", "version": "0.1.0" }
```

**语义保证**：只反映**进程存活**，不探测 MySQL / Redis（避免探活把负载打到下游）。依赖健康由系统测试与 `/readyz` 之外的运维手段覆盖。

---

## CT-004 跨域（CORS）

- 开发环境：允许 `http://localhost:5173` / `http://127.0.0.1:5173`（Vite 默认端口）。
- 允许来源通过 `Settings.cors_origins` 配置，**默认不是 `*`**（源项目用 `*` + `allow_credentials=True`，是无效且不安全的组合，**刻意差异**）。
- 生产环境下前端经同源反向代理访问 `/api`，不依赖 CORS。

---

## CT-005 错误体形状

- 业务错误统一 `{"detail": "<人类可读信息>"}`。
- 校验错误沿用 FastAPI 默认的 `{"detail": [...]}` 数组形状，前端按 `detail` 存在性判断即可。
- **任何**响应都不得包含：堆栈、SQL、连接串、密钥、模型 API 错误原文（可保留错误类型与摘要）。

---

## CT-006 前端契约映射（`src/web`）

| 前端位置 | 契约 |
|---|---|
| `src/api/crew.ts` | `submitCrew()` → `CT-001`；`fetchCrewJob()` → `CT-002` |
| `src/types.ts` | `KnownJobStatus`、`CrewJobResponse`、`JobEvent` 与本节字段一一对应 |
| 轮询 | 3 秒间隔；`status` 进入 `COMPLETE`/`ERROR` **立即停止**（`REQ-012`） |
| 错误展示 | 优先取 `detail`（字符串或数组首项的 `msg`），否则 `HTTP <status>` |
