import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, extractErrorDetail, fetchCrewJob, isUnrecoverableError, submitCrew } from '@/api/crew'

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('extractErrorDetail（CT-005 / CT-006）', () => {
  it('业务错误：取字符串 detail', () => {
    expect(extractErrorDetail({ detail: '任务队列不可用' })).toBe('任务队列不可用')
  })

  it('校验错误：取 FastAPI detail 数组首项的 msg', () => {
    const body = {
      detail: [
        { loc: ['body', 'project_description'], msg: 'String should have at least 10 characters', type: 'value_error' },
        { loc: ['body', 'customer_domain'], msg: '另一个错误', type: 'value_error' },
      ],
    }
    expect(extractErrorDetail(body)).toBe('String should have at least 10 characters')
  })

  it('没有可用信息时返回空串，由调用方回退到 HTTP <status>', () => {
    expect(extractErrorDetail({})).toBe('')
    expect(extractErrorDetail({ detail: [] })).toBe('')
    expect(extractErrorDetail({ detail: 42 })).toBe('')
    expect(extractErrorDetail('<html>502 Bad Gateway</html>')).toBe('')
    expect(extractErrorDetail(null)).toBe('')
  })
})

describe('API 客户端', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('CT-001：202 返回 job_id', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ job_id: 'job-1' }, 202))
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      submitCrew({ customer_domain: 'openai.com', project_description: '为 AI 公司制定营销方案' }),
    ).resolves.toBe('job-1')

    expect(fetchMock).toHaveBeenCalledWith('/api/crew', expect.objectContaining({ method: 'POST' }))
  })

  it('CT-001：422 抛出数组首项 msg，而不是 "HTTP 422"', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse({ detail: [{ loc: [], msg: '入参非法：项目描述过短' }] }, 422)),
    )

    await expect(
      submitCrew({ customer_domain: 'a', project_description: 'b' }),
    ).rejects.toThrow('入参非法：项目描述过短')
  })

  it('CT-005：detail 缺失时回退为 HTTP <status>（响应体不是 JSON 也一样）', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('Bad Gateway', { status: 502 })))

    await expect(fetchCrewJob('job-1')).rejects.toThrow('HTTP 502')
  })

  it('CT-002：404 抛出带 status 的 ApiError，供轮询层判断「是否停止」', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: 'job not found' }, 404)))

    const error: unknown = await fetchCrewJob('missing').catch((cause: unknown) => cause)

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 404, message: 'job not found' })
    expect(isUnrecoverableError(error)).toBe(true)
  })

  it('CT-005：503 是暂时性故障，不判定为不可恢复', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: '任务队列不可用' }, 503)))

    const error: unknown = await fetchCrewJob('job-1').catch((cause: unknown) => cause)

    expect(error).toBeInstanceOf(ApiError)
    expect(isUnrecoverableError(error)).toBe(false)
  })

  it('没有状态码的错误一律按暂时性处理（不做错误消息的字符串匹配）', () => {
    expect(isUnrecoverableError(new TypeError('Failed to fetch'))).toBe(false)
    expect(isUnrecoverableError(new Error('HTTP 404'))).toBe(false)
    expect(isUnrecoverableError(undefined)).toBe(false)
  })

  it('CT-002：200 返回作业详情，job_id 做 URL 编码', async () => {
    const payload = { job_id: 'job/1', status: 'RUNNING', result: null, events: [] }
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(payload, 200))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchCrewJob('job/1')).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith('/api/crew/job%2F1')
  })
})
