/**
 * job_id 与地址栏 query 的同步（REQ-012：刷新不丢、链接可分享）。
 *
 * 抽成独立模块是为了能脱离组件单测：这里只做「读地址 / 写地址」一件事。
 */

/** URL 中承载 job_id 的 query 键 */
export const JOB_ID_QUERY_KEY = 'job_id'

/** 读取地址栏里的 job_id；没有值或不在浏览器环境时返回空串 */
export function readJobIdFromUrl(): string {
  if (typeof window === 'undefined') return ''
  return (new URL(window.location.href).searchParams.get(JOB_ID_QUERY_KEY) ?? '').trim()
}

/**
 * 把 job_id 写回地址栏。
 *
 * 用 `replaceState` 而非 `pushState`：轮询期间 job_id 只会被设置一次，
 * 但回填 / 清空都不该在浏览历史里留下垃圾记录。传空串表示移除该参数。
 */
export function writeJobIdToUrl(id: string): void {
  if (typeof window === 'undefined') return
  const url = new URL(window.location.href)
  if (id === '') url.searchParams.delete(JOB_ID_QUERY_KEY)
  else url.searchParams.set(JOB_ID_QUERY_KEY, id)
  window.history.replaceState(null, '', url.toString())
}
