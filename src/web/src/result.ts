/**
 * 把 `result` 归一化成可渲染的视图模型。
 *
 * 契约（CT-002）规定：COMPLETE → 结构化对象，ERROR → 字符串，其余 → null。
 * 但这三种形状都可能因为后端演进或异常而偏离，**不能假设它一定是对象**
 * （源项目就是在这里踩的坑：直接当成对象渲染，遇到字符串就崩）。
 * 因此这里对 `unknown` 做穷尽分支，任何形状都有兜底展示，绝不丢数据。
 */

export interface ResultField {
  label: string
  value: string
}

export interface ResultGroup {
  label: string
  items: string[]
}

export interface ResultBlock {
  /** 稳定标识，供 v-for 的 key 使用 */
  key: string
  title: string
  /** 长文本正文（调研报告等） */
  text: string
  fields: ResultField[]
  groups: ResultGroup[]
}

export type ResultView =
  /** PENDING / RUNNING：结果尚未产生 */
  | { kind: 'none' }
  /** 纯字符串（ERROR 的错误摘要） */
  | { kind: 'text'; text: string }
  /** 结构化结果，按 FlowResult 的四个键分块 */
  | { kind: 'blocks'; blocks: ResultBlock[] }
  /** 形状未知：原样展示 JSON，保证信息不丢 */
  | { kind: 'raw'; text: string }

/** 四个已知顶层键（spec/020 §2.3 FlowResult） */
const RESEARCH_REPORT = 'research_report'
const STRATEGY = 'strategy'
const CAMPAIGN_IDEA = 'campaign_idea'
const COPY = 'copy'

const STRATEGY_TITLES: Record<string, string> = {
  name: '战略名称',
  tactics: '战术',
  channels: '渠道',
  kpis: '关键绩效指标',
}

const CAMPAIGN_TITLES: Record<string, string> = {
  name: '创意名称',
  description: '创意说明',
  audience: '目标受众',
  channel: '投放渠道',
}

const COPY_TITLES: Record<string, string> = {
  title: '标题',
  body: '正文',
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** 标量转文本；null/undefined/空串一律返回空串，由调用方决定跳过 */
function asText(value: unknown): string {
  if (typeof value === 'string') return value.trim()
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return ''
}

function asTextList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map(asText).filter((item) => item !== '')
}

/** JSON 兜底序列化：循环引用等异常也不能让页面白屏 */
function safeJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? String(value)
  } catch {
    return String(value)
  }
}

/**
 * 把一个对象摊平成「名值对 + 列表」。
 *
 * 按对象自身的键顺序遍历：已知键用中文标签，未知键保留原始键名，
 * 嵌套对象退化为 JSON 文本——宁可少排版，不可丢数据。
 */
function describeObject(
  value: Record<string, unknown>,
  titles: Record<string, string>,
): { fields: ResultField[]; groups: ResultGroup[] } {
  const fields: ResultField[] = []
  const groups: ResultGroup[] = []

  for (const [key, raw] of Object.entries(value)) {
    const label = titles[key] ?? key

    if (Array.isArray(raw)) {
      const items = asTextList(raw)
      if (items.length > 0) groups.push({ label, items })
      continue
    }

    if (isRecord(raw)) {
      fields.push({ label, value: safeJson(raw) })
      continue
    }

    const text = asText(raw)
    if (text !== '') fields.push({ label, value: text })
  }

  return { fields, groups }
}

function buildBlock(key: string, title: string, value: unknown, titles: Record<string, string>): ResultBlock {
  const { fields, groups } = describeObject(value as Record<string, unknown>, titles)
  return { key, title, text: '', fields, groups }
}

/** 把 CT-002 的 `result` 字段转成视图模型 */
export function buildResultView(result: unknown): ResultView {
  if (result === null || result === undefined) return { kind: 'none' }

  if (typeof result === 'string') {
    const text = result.trim()
    return text === '' ? { kind: 'none' } : { kind: 'text', text }
  }

  if (!isRecord(result)) return { kind: 'raw', text: safeJson(result) }

  const blocks: ResultBlock[] = []

  const report = result[RESEARCH_REPORT]
  if (typeof report === 'string' && report.trim() !== '') {
    blocks.push({ key: RESEARCH_REPORT, title: '调研报告', text: report.trim(), fields: [], groups: [] })
  }

  const strategy = result[STRATEGY]
  if (isRecord(strategy)) {
    blocks.push(buildBlock(STRATEGY, '市场战略', strategy, STRATEGY_TITLES))
  }

  const campaignIdea = result[CAMPAIGN_IDEA]
  if (isRecord(campaignIdea)) {
    blocks.push(buildBlock(CAMPAIGN_IDEA, '活动创意', campaignIdea, CAMPAIGN_TITLES))
  }

  const copy = result[COPY]
  if (isRecord(copy)) {
    blocks.push(buildBlock(COPY, '广告文案', copy, COPY_TITLES))
  }

  // 一个已知键都没命中（后端形状变了）→ 原样展示 JSON，别让用户看到空白或误导性的分块
  if (blocks.length === 0) return { kind: 'raw', text: safeJson(result) }

  const known: readonly string[] = [RESEARCH_REPORT, STRATEGY, CAMPAIGN_IDEA, COPY]
  const extras: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(result)) {
    if (!known.includes(key)) extras[key] = value
  }
  if (Object.keys(extras).length > 0) {
    blocks.push({ key: '__extras__', title: '其他字段', text: safeJson(extras), fields: [], groups: [] })
  }

  return { kind: 'blocks', blocks }
}
