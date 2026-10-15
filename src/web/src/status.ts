import type { JobStatus } from '@/types'

/** 终态（spec/020 §1.1）：到达后状态与 result 永不再变，轮询必须立即停止 */
export const TERMINAL_STATUSES = ['COMPLETE', 'ERROR'] as const

/**
 * 是否为终态。
 *
 * 大小写与未知状态都容错：CT-002 明确「新增状态不视为破坏性变更」，
 * 因此除 COMPLETE / ERROR 之外的一律视为「还在跑」。
 */
export function isTerminalStatus(status: JobStatus | null | undefined): boolean {
  if (typeof status !== 'string') return false
  const normalized = status.trim().toUpperCase()
  return (TERMINAL_STATUSES as readonly string[]).includes(normalized)
}
