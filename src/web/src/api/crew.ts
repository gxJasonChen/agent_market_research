import type { CrewJobResponse, CrewRequest, CrewSubmitResponse } from '@/types'

/**
 * 后端基址（CT-006）。
 *
 * - 开发环境留空：走 vite proxy（见 vite.config.ts 的 server.proxy），前端只写 `/api/...`；
 * - 需要直连时设 `VITE_API_BASE`，例如 `VITE_API_BASE=http://127.0.0.1:8012`。
 * 这里只放地址，不放任何密钥——前端产物是公开的。
 */
const API_BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/+$/, '')

/**
 * 后端返回非 2xx 时抛出。
 *
 * 带上 `status` 是为了让调用方按**状态码**做决策（例如 404 停止轮询、503 继续重试），
 * 而不是去匹配错误消息——消息是写给人看的，措辞随时会变（CT-006 轮询停止规则）。
 */
export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/**
 * 是否为「重试也不会改变结果」的错误（CT-006：`404` 作业不存在）。
 *
 * 其余情况——`503`、网络中断（fetch 直接 reject 的 TypeError）等——一律当作暂时性故障，
 * 由调用方继续重试；没有状态码的错误不会被误判为不可恢复。
 */
export function isUnrecoverableError(cause: unknown): boolean {
  return cause instanceof ApiError && cause.status === 404
}

/**
 * 从错误响应体里提取人类可读信息（CT-005）。
 *
 * `detail` 有两种形状：业务错误的字符串，以及 FastAPI 校验错误的数组
 * （`[{loc, msg, type}, ...]`，数组时取首项的 `msg`）。都不是则返回空串，
 * 由调用方回退到 `HTTP <status>`。
 */
export function extractErrorDetail(body: unknown): string {
  if (typeof body !== 'object' || body === null) return ''

  const detail = (body as { detail?: unknown }).detail
  if (typeof detail === 'string') return detail.trim()

  if (Array.isArray(detail)) {
    const first: unknown = detail[0]
    if (typeof first === 'string') return first.trim()
    if (typeof first === 'object' && first !== null) {
      const msg = (first as { msg?: unknown }).msg
      if (typeof msg === 'string') return msg.trim()
    }
  }

  return ''
}

/** 把非 2xx 响应转成可展示的错误信息：优先 `detail`，否则 `HTTP <status>` */
async function readError(res: Response): Promise<string> {
  try {
    const detail = extractErrorDetail(await res.json())
    if (detail) return detail
  } catch {
    // 响应体不是 JSON（网关错误页等），交给下面的状态码兜底
  }
  return `HTTP ${res.status}`
}

/** POST /api/crew —— 提交作业（CT-001），返回 job_id */
export async function submitCrew(payload: CrewRequest): Promise<string> {
  const res = await fetch(`${API_BASE}/api/crew`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })

  // 带上状态码：调用方要按状态码决策（503 重试 / 404 停止），不能靠匹配消息文本
  if (!res.ok) throw new ApiError(res.status, await readError(res))

  const data = (await res.json()) as Partial<CrewSubmitResponse>
  if (!data.job_id) throw new ApiError(res.status, '服务端未返回 job_id')

  return data.job_id
}

/** GET /api/crew/{job_id} —— 查询作业状态、结果与事件流水（CT-002） */
export async function fetchCrewJob(jobId: string): Promise<CrewJobResponse> {
  const res = await fetch(`${API_BASE}/api/crew/${encodeURIComponent(jobId)}`)
  if (!res.ok) throw new ApiError(res.status, await readError(res))
  return (await res.json()) as CrewJobResponse
}
