/**
 * 与后端 FastAPI 的契约映射（spec/022 CT-006）。
 *
 * POST /api/crew            → CrewSubmitResponse（CT-001）
 * GET  /api/crew/{job_id}   → CrewJobResponse（CT-002）
 */

/** 后端已知的作业状态（spec/020 §1.1） */
export type KnownJobStatus = 'PENDING' | 'RUNNING' | 'COMPLETE' | 'ERROR'

/**
 * 契约规定「新增状态不视为破坏性变更」（CT-002），因此状态按字符串容错：
 * 前端只对 COMPLETE / ERROR 做终态判定，其余一律继续轮询。
 */
export type JobStatus = KnownJobStatus | (string & {})

/** POST /api/crew 的请求体（CT-001） */
export interface CrewRequest {
  /** 客户领域 / 官网，strip 后 3–255 字符 */
  customer_domain: string
  /** 项目背景与目标，strip 后 10–4000 字符 */
  project_description: string
}

/** 事件流水中的一条记录（CT-002） */
export interface JobEvent {
  /** ISO-8601 带时区偏移 */
  timestamp: string
  data: string
}

/** GET /api/crew/{job_id} 的响应体（CT-002） */
export interface CrewJobResponse {
  job_id: string
  status: JobStatus
  /**
   * COMPLETE → 结构化对象（FlowResult）；ERROR → 错误摘要字符串；PENDING/RUNNING → null。
   * 契约如此，但渲染前仍必须做类型判断——不能假设它一定是对象。
   */
  result: unknown
  /** 已按 timestamp 正序（最早在前） */
  events: JobEvent[]
}

/** POST /api/crew 的响应体（CT-001，202） */
export interface CrewSubmitResponse {
  job_id: string
}

/** 阶段二：营销战略（spec/020 §2.1） */
export interface MarketStrategy {
  name: string
  tactics: string[]
  channels: string[]
  kpis: string[]
}

/** 阶段二：活动创意（spec/020 §2.1） */
export interface CampaignIdea {
  name: string
  description: string
  audience: string
  channel: string
}

/** 阶段二：广告文案（spec/020 §2.1） */
export interface Copy {
  title: string
  body: string
}

/** COMPLETE 时的结构化结果（spec/020 §2.3） */
export interface FlowResult {
  research_report: string
  strategy: MarketStrategy
  campaign_idea: CampaignIdea
  copy: Copy
}
