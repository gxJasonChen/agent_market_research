import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/crew'
import { useJobPolling } from '@/composables/useJobPolling'
import type { CrewJobResponse } from '@/types'

/** 构造一份契约形状（CT-002）的作业响应 */
function jobResponse(overrides: Partial<CrewJobResponse> = {}): CrewJobResponse {
  return { job_id: 'job-1', status: 'RUNNING', result: null, events: [], ...overrides }
}

type Fetcher = (jobId: string) => Promise<CrewJobResponse>

/** 让 mock 的 Promise 链跑完：fake timers 下微任务不会自己推进 */
async function flush(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0)
}

describe('useJobPolling', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('REQ-012：默认每 3 秒拉取一次作业状态', async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(jobResponse())
    const polling = useJobPolling({ fetcher })

    polling.start('job-1')
    await flush()
    // start 后立即拉一次，避免首个 3 秒里界面空白
    expect(fetcher).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(2999)
    expect(fetcher).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(1)
    expect(fetcher).toHaveBeenCalledTimes(2)

    await vi.advanceTimersByTimeAsync(3000)
    expect(fetcher).toHaveBeenCalledTimes(3)

    expect(fetcher).toHaveBeenLastCalledWith('job-1')
    polling.stop()
  })

  it('REQ-012：状态变为 COMPLETE 后立即停止轮询，不再发起请求', async () => {
    const fetcher = vi
      .fn<Fetcher>()
      .mockResolvedValueOnce(jobResponse({ status: 'RUNNING' }))
      .mockResolvedValue(jobResponse({ status: 'COMPLETE', result: { research_report: '报告' } }))
    const polling = useJobPolling({ fetcher })

    polling.start('job-1')
    await flush()
    await vi.advanceTimersByTimeAsync(3000)
    // 第二次拉取拿到终态
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(polling.polling.value).toBe(false)
    expect(polling.isTerminal.value).toBe(true)
    expect(polling.job.value?.status).toBe('COMPLETE')

    await vi.advanceTimersByTimeAsync(30_000)
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('REQ-012：状态变为 ERROR 后同样立即停止轮询', async () => {
    const fetcher = vi
      .fn<Fetcher>()
      .mockResolvedValueOnce(jobResponse({ status: 'PENDING' }))
      .mockResolvedValue(jobResponse({ status: 'ERROR', result: '模型调用失败' }))
    const polling = useJobPolling({ fetcher })

    polling.start('job-1')
    await flush()
    await vi.advanceTimersByTimeAsync(3000)
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(polling.polling.value).toBe(false)
    expect(polling.job.value?.result).toBe('模型调用失败')

    await vi.advanceTimersByTimeAsync(30_000)
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('REQ-011：请求出错时不静默停死——暴露错误并在下一个 tick 重试', async () => {
    const fetcher = vi
      .fn<Fetcher>()
      .mockRejectedValueOnce(new Error('HTTP 503'))
      .mockResolvedValue(jobResponse({ status: 'RUNNING' }))
    const polling = useJobPolling({ fetcher })

    polling.start('job-1')
    await flush()
    expect(fetcher).toHaveBeenCalledTimes(1)
    // 错误必须让上层看得见，而不是被吞掉
    expect(polling.error.value).toBe('HTTP 503')
    // 出错不停轮询
    expect(polling.polling.value).toBe(true)

    await vi.advanceTimersByTimeAsync(3000)
    expect(fetcher).toHaveBeenCalledTimes(2)
    // 成功一次后清掉错误
    expect(polling.error.value).toBe('')
    expect(polling.job.value?.status).toBe('RUNNING')

    polling.stop()
  })

  it('stop() 之后不再发起请求，且轮询标记复位', async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(jobResponse())
    const polling = useJobPolling({ fetcher })

    polling.start('job-1')
    await flush()
    polling.stop()
    expect(polling.polling.value).toBe(false)

    await vi.advanceTimersByTimeAsync(30_000)
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('未知状态不当作终态，继续轮询（CT-002：新增状态不视为破坏性变更）', async () => {
    const fetcher = vi
      .fn<Fetcher>()
      .mockResolvedValueOnce(jobResponse({ status: 'QUEUED' }))
      .mockResolvedValue(jobResponse({ status: 'QUEUED' }))
    const polling = useJobPolling({ fetcher })

    polling.start('job-1')
    await flush()
    await vi.advanceTimersByTimeAsync(3000)

    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(polling.polling.value).toBe(true)
    expect(polling.error.value).toBe('')

    polling.stop()
  })

  it('REQ-012：作业不存在（404）→ 停止轮询，不再每 3 秒白刷', async () => {
    const fetcher = vi.fn<Fetcher>().mockRejectedValue(new ApiError(404, 'job not found'))
    const polling = useJobPolling({ fetcher })

    polling.start('job-404')
    await flush()
    expect(fetcher).toHaveBeenCalledTimes(1)
    // 停止，但错误必须留在界面上（"停止并提示"）
    expect(polling.polling.value).toBe(false)
    expect(polling.error.value).toBe('job not found')

    await vi.advanceTimersByTimeAsync(30_000)
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('REQ-012：暂时性错误（503）→ 不停止轮询，下个周期照常重试', async () => {
    const fetcher = vi.fn<Fetcher>().mockRejectedValue(new ApiError(503, '任务队列不可用'))
    const polling = useJobPolling({ fetcher })

    polling.start('job-1')
    await flush()
    expect(polling.error.value).toBe('任务队列不可用')
    expect(polling.polling.value).toBe(true)

    await vi.advanceTimersByTimeAsync(3000)
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(polling.polling.value).toBe(true)

    polling.stop()
  })

  it('REQ-012：网络错误（fetch 直接抛 TypeError，没有状态码）也按暂时性处理', async () => {
    const fetcher = vi.fn<Fetcher>().mockRejectedValue(new TypeError('Failed to fetch'))
    const polling = useJobPolling({ fetcher })

    polling.start('job-1')
    await flush()
    expect(polling.polling.value).toBe(true)

    await vi.advanceTimersByTimeAsync(3000)
    expect(fetcher).toHaveBeenCalledTimes(2)

    polling.stop()
  })

  it('REQ-012：404 停止之后用户仍能手动 refresh() 走出，不会被卡死', async () => {
    const fetcher = vi
      .fn<Fetcher>()
      .mockRejectedValueOnce(new ApiError(404, 'job not found'))
      .mockResolvedValue(jobResponse({ status: 'RUNNING' }))
    const polling = useJobPolling({ fetcher })

    polling.start('job-404')
    await flush()
    expect(polling.polling.value).toBe(false)

    // 停止只是停掉定时器，手动查询这条路必须还通着
    await polling.refresh()
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(polling.job.value?.status).toBe('RUNNING')
    expect(polling.error.value).toBe('')

    // reset() 是另一条出路
    polling.reset()
    expect(polling.jobId.value).toBe('')
    expect(polling.job.value).toBeNull()
  })

  it('refresh() 手动拉取一次，不改变轮询开关', async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(jobResponse())
    const polling = useJobPolling({ fetcher })

    polling.start('job-1')
    await flush()
    polling.stop()

    await polling.refresh()
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(polling.polling.value).toBe(false)
    expect(polling.lastSyncedAt.value).not.toBeNull()
  })

  it('未指定 job_id 时 refresh() 不发请求', async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(jobResponse())
    const polling = useJobPolling({ fetcher })

    await polling.refresh()
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('reset() 清空作业与错误，并停止轮询', async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(jobResponse())
    const polling = useJobPolling({ fetcher })

    polling.start('job-1')
    await flush()
    polling.reset()

    expect(polling.job.value).toBeNull()
    expect(polling.jobId.value).toBe('')
    expect(polling.polling.value).toBe(false)

    await vi.advanceTimersByTimeAsync(30_000)
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
})
