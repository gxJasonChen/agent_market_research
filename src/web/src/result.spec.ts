import { describe, expect, it } from 'vitest'

import { buildResultView } from '@/result'

/** 一份符合 spec/020 §2.3 的完整结构化结果 */
const FLOW_RESULT = {
  research_report: '市场调研：企业客户对分布式消息中间件的认知集中在……',
  strategy: {
    name: '开发者社区优先',
    tactics: ['开源布道', '技术白皮书'],
    channels: ['GitHub', '技术大会'],
    kpis: ['注册开发者数', '试用转化率'],
  },
  campaign_idea: {
    name: '万物互联挑战赛',
    description: '面向物联网开发者的一场实战挑战赛',
    audience: '物联网解决方案架构师',
    channel: '线上直播 + 社区',
  },
  copy: { title: '让每一台设备都开口说话', body: '从毫秒级延迟开始……' },
}

describe('buildResultView', () => {
  it('REQ-012：PENDING / RUNNING 的 null → 无可展示结果', () => {
    expect(buildResultView(null).kind).toBe('none')
    expect(buildResultView(undefined).kind).toBe('none')
  })

  it('REQ-012：ERROR 的字符串 → 纯文本展示', () => {
    const view = buildResultView('模型调用失败：连接超时')
    expect(view).toEqual({ kind: 'text', text: '模型调用失败：连接超时' })
  })

  it('REQ-012：COMPLETE 的结构化对象 → 按四个键分块展示', () => {
    const view = buildResultView(FLOW_RESULT)
    expect(view.kind).toBe('blocks')
    if (view.kind !== 'blocks') return

    expect(view.blocks.map((block) => block.key)).toEqual([
      'research_report',
      'strategy',
      'campaign_idea',
      'copy',
    ])
    expect(view.blocks.map((block) => block.title)).toEqual(['调研报告', '市场战略', '活动创意', '广告文案'])
    expect(view.blocks[0]?.text).toContain('市场调研')
    expect(view.blocks[1]?.groups).toEqual([
      { label: '战术', items: ['开源布道', '技术白皮书'] },
      { label: '渠道', items: ['GitHub', '技术大会'] },
      { label: '关键绩效指标', items: ['注册开发者数', '试用转化率'] },
    ])
    expect(view.blocks[1]?.fields).toEqual([{ label: '战略名称', value: '开发者社区优先' }])
    expect(view.blocks[3]?.fields).toEqual([
      { label: '标题', value: '让每一台设备都开口说话' },
      { label: '正文', value: '从毫秒级延迟开始……' },
    ])
  })

  it('REQ-012：结构变化时不崩——已知键缺一半也能渲染出剩下的块', () => {
    const view = buildResultView({ research_report: '只有调研报告', copy: { title: '标题' } })
    expect(view.kind).toBe('blocks')
    if (view.kind !== 'blocks') return
    expect(view.blocks.map((block) => block.key)).toEqual(['research_report', 'copy'])
    expect(view.blocks[1]?.fields).toEqual([{ label: '标题', value: '标题' }])
  })

  it('REQ-012：result 是对象但形状未知 → 原样展示 JSON，不丢数据', () => {
    const view = buildResultView({ answer: 42, nested: { a: 1 } })
    expect(view.kind).toBe('raw')
    if (view.kind !== 'raw') return
    expect(view.text).toContain('"answer": 42')
    expect(view.text).toContain('"nested"')
  })

  it('REQ-012：已知键里混入未知字段 → 追加「其他字段」块，不静默丢弃', () => {
    const view = buildResultView({ ...FLOW_RESULT, trace_id: 'abc-123' })
    expect(view.kind).toBe('blocks')
    if (view.kind !== 'blocks') return
    const extras = view.blocks.find((block) => block.key === '__extras__')
    expect(extras?.text).toContain('abc-123')
  })

  it('REQ-012：result 不是对象也不是字符串（数组 / 数字 / 布尔）也能安全渲染', () => {
    expect(buildResultView([1, 2, 3])).toEqual({ kind: 'raw', text: '[\n  1,\n  2,\n  3\n]' })
    expect(buildResultView(0).kind).toBe('raw')
    expect(buildResultView(false).kind).toBe('raw')
    expect(buildResultView('   ').kind).toBe('none')
  })
})
