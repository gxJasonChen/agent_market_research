import { describe, expect, it } from 'vitest'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'

import App from '@/App.vue'
import StatusBadge from '@/components/StatusBadge.vue'

/**
 * 冒烟测试：type-check 与 build 都发现不了模板里的运行时错误
 * （引用了不存在的变量、组件注册失败等），这里用 SSR 把整棵树跑一遍——
 * 不需要 jsdom，也不引入任何测试专用依赖。
 */
describe('App.vue 冒烟', () => {
  it('REQ-012：首屏渲染出提交表单（两个字段 + 校验提示）与空状态', async () => {
    const html = await renderToString(createSSRApp(App))

    expect(html).toContain('营销内容生成控制台')
    expect(html).toContain('客户领域 / 官网')
    expect(html).toContain('项目描述')
    expect(html).toContain('3–255 个字符')
    expect(html).toContain('10–4000 个字符')
    expect(html).toContain('提交作业')
    // 没有 job_id 时不渲染状态区，而是给出空状态说明
    expect(html).toContain('提交作业或按 job_id 查询后')
  })
})

describe('StatusBadge（REQ-012 状态徽章）', () => {
  async function renderBadge(status: string): Promise<string> {
    return renderToString(createSSRApp({ render: () => h(StatusBadge, { status }) }))
  }

  it('四个状态各有中文标签，运行中带 running 类（呼吸动效挂在这个类上）', async () => {
    await expect(renderBadge('PENDING')).resolves.toContain('排队中')
    await expect(renderBadge('COMPLETE')).resolves.toContain('已完成')
    await expect(renderBadge('ERROR')).resolves.toContain('失败')

    const running = await renderBadge('RUNNING')
    expect(running).toContain('运行中')
    expect(running).toContain('badge running')
  })

  it('CT-002：未知状态原样展示，不被前端吞掉', async () => {
    const html = await renderBadge('QUEUED')
    expect(html).toContain('QUEUED')
    expect(html).toContain('badge unknown')
  })
})
