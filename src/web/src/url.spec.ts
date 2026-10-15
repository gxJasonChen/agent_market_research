import { afterEach, describe, expect, it, vi } from 'vitest'

import { readJobIdFromUrl, writeJobIdToUrl } from '@/url'

interface FakeWindow {
  location: { href: string }
  history: { replaceState: (data: unknown, title: string, url: string) => void }
}

/** 用最小替身代替真实浏览器环境：只需要 location.href 与 history.replaceState */
function stubWindow(href: string): FakeWindow {
  const fake: FakeWindow = {
    location: { href },
    history: {
      replaceState: (_data: unknown, _title: string, url: string) => {
        fake.location.href = url
      },
    },
  }
  vi.stubGlobal('window', fake)
  return fake
}

describe('job_id 与 URL query 的同步（REQ-012）', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('有 job_id 时读取出来', () => {
    stubWindow('http://127.0.0.1:5173/?job_id=job-1')
    expect(readJobIdFromUrl()).toBe('job-1')
  })

  it('没有 job_id 时返回空串', () => {
    stubWindow('http://127.0.0.1:5173/')
    expect(readJobIdFromUrl()).toBe('')
  })

  it('写入 job_id 时保留已有 query 参数', () => {
    const fake = stubWindow('http://127.0.0.1:5173/?from=share')
    writeJobIdToUrl('job-1')

    const url = new URL(fake.location.href)
    expect(url.searchParams.get('job_id')).toBe('job-1')
    expect(url.searchParams.get('from')).toBe('share')
  })

  it('写入后能读回来（刷新回填 / 分享链接的往返）', () => {
    stubWindow('http://127.0.0.1:5173/')
    writeJobIdToUrl('job-2')
    expect(readJobIdFromUrl()).toBe('job-2')
  })

  it('传空串时移除参数', () => {
    const fake = stubWindow('http://127.0.0.1:5173/?job_id=job-1&from=share')
    writeJobIdToUrl('')
    expect(fake.location.href).not.toContain('job_id')
    expect(readJobIdFromUrl()).toBe('')
  })

  it('非浏览器环境（SSR / 单测）下两个函数都安全短路', () => {
    expect(readJobIdFromUrl()).toBe('')
    expect(() => writeJobIdToUrl('job-1')).not.toThrow()
  })
})
