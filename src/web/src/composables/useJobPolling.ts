import { computed, getCurrentScope, onScopeDispose, ref, type ComputedRef, type Ref } from 'vue'

import { fetchCrewJob, isUnrecoverableError } from '@/api/crew'
import { isTerminalStatus } from '@/status'
import type { CrewJobResponse } from '@/types'

/** 轮询间隔：契约（CT-006 / REQ-012）要求每 3 秒一次 */
export const DEFAULT_POLL_INTERVAL_MS = 3000

export interface UseJobPollingOptions {
  /** 拉取作业详情，默认走 CT-002 的真实实现；测试注入替身 */
  fetcher?: (jobId: string) => Promise<CrewJobResponse>
  /** 轮询间隔（毫秒），默认 3000 */
  intervalMs?: number
}

export interface JobPolling {
  /** 最近一次成功拿到的作业；尚未拿到为 null */
  job: Ref<CrewJobResponse | null>
  /** 当前轮询的 job_id，空串表示没有在轮询 */
  jobId: Ref<string>
  /** 是否处于轮询中 */
  polling: Ref<boolean>
  /** 是否有请求在途 */
  loading: Ref<boolean>
  /** 最近一次错误信息；成功同步一次后清空 */
  error: Ref<string>
  /** 已发起的请求次数（含首拉），便于回归测试断言 */
  requestCount: Ref<number>
  /** 最近一次成功同步的时间 */
  lastSyncedAt: Ref<Date | null>
  /** 当前作业是否已到终态（COMPLETE / ERROR） */
  isTerminal: ComputedRef<boolean>
  /** 开始轮询：立即拉一次，随后每 intervalMs 一次 */
  start: (jobId: string) => void
  /** 停止轮询（幂等） */
  stop: () => void
  /** 立即拉一次，不改变轮询开关 */
  refresh: () => Promise<void>
  /** 停止并清空全部状态 */
  reset: () => void
}

/**
 * 作业状态轮询。
 *
 * 停止规则见 CT-006 / REQ-012，四种情况分得很清：
 * 1. 默认 3 秒一次（提交后先立即拉一次，避免首个 3 秒界面空白）；
 * 2. `status` 进入 COMPLETE / ERROR → 停止（终态不会再变，REQ-004）；
 * 3. `404` 作业不存在 → 停止。重试不会让不存在的作业出现，
 *    继续轮询只会每 3 秒白刷一次请求（源项目的痛点之一就是无效轮询）；
 * 4. `503` / 网络错误等暂时性故障 → **不停**，把错误暴露给界面，下个 tick 继续重试，
 *    否则后端重启这类瞬时故障会让页面永远停在旧状态上。
 */
export function useJobPolling(options: UseJobPollingOptions = {}): JobPolling {
  const intervalMs = options.intervalMs ?? DEFAULT_POLL_INTERVAL_MS
  const fetcher = options.fetcher ?? fetchCrewJob

  const jobId = ref('')
  const job = ref<CrewJobResponse | null>(null)
  const polling = ref(false)
  const loading = ref(false)
  const error = ref('')
  const requestCount = ref(0)
  const lastSyncedAt = ref<Date | null>(null)

  let timer: ReturnType<typeof setInterval> | null = null
  /** 防止慢请求与下一个 tick 叠在一起（同一时刻只允许一个在途请求） */
  let inFlight = false

  const isTerminal = computed(() => job.value !== null && isTerminalStatus(job.value.status))

  function stop(): void {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
    polling.value = false
  }

  async function refresh(): Promise<void> {
    const id = jobId.value
    if (id === '' || inFlight) return

    inFlight = true
    loading.value = true
    requestCount.value += 1
    try {
      const next = await fetcher(id)
      job.value = next
      lastSyncedAt.value = new Date()
      error.value = ''
      if (isTerminalStatus(next.status)) stop()
    } catch (cause) {
      // 不吞异常：转成界面可读的信息，并保留已有结果
      error.value = cause instanceof Error ? cause.message : String(cause)
      // 不可恢复的错误（404）才停表；暂时性故障继续重试，见上面第 4 条
      if (isUnrecoverableError(cause)) stop()
    } finally {
      inFlight = false
      loading.value = false
    }
  }

  function start(id: string): void {
    stop()
    jobId.value = id
    job.value = null
    lastSyncedAt.value = null
    error.value = ''
    requestCount.value = 0
    polling.value = true
    // 先立刻拉一次，避免首个 3 秒里界面一片空白；若首拉就是终态，stop() 会清掉下面这个定时器
    void refresh()
    timer = setInterval(() => {
      void refresh()
    }, intervalMs)
  }

  function reset(): void {
    stop()
    jobId.value = ''
    job.value = null
    lastSyncedAt.value = null
    error.value = ''
    requestCount.value = 0
  }

  // 组件卸载时自动停表；在没有 effect scope 的环境（如单测直接调用）里跳过
  if (getCurrentScope() !== undefined) onScopeDispose(stop)

  return {
    job,
    jobId,
    polling,
    loading,
    error,
    requestCount,
    lastSyncedAt,
    isTerminal,
    start,
    stop,
    refresh,
    reset,
  }
}
