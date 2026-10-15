<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'

import { submitCrew } from '@/api/crew'
import StatusBadge from '@/components/StatusBadge.vue'
import { useJobPolling } from '@/composables/useJobPolling'
import { buildResultView } from '@/result'
import { readJobIdFromUrl, writeJobIdToUrl } from '@/url'

/** 输入长度边界，与 CT-001 / REQ-010 逐字一致（strip 后计数） */
const DOMAIN_MIN = 3
const DOMAIN_MAX = 255
const DESC_MIN = 10
const DESC_MAX = 4000

const SAMPLE = {
  customer_domain: 'openai.com',
  project_description: '为一家 AI 基础设施公司制定新品发布营销方案，目标是让企业客户了解产品的差异化能力。',
}

const form = reactive({ ...SAMPLE })
/** 离开输入框或尝试提交后才提示，避免刚打开页面就一片红 */
const touched = reactive({ customer_domain: false, project_description: false })
const submitted = ref(false)

const submitting = ref(false)
const submitError = ref('')
const lookupId = ref('')
const copyHint = ref('')

// 解构出来，模板里才能自动解包 ref
const {
  job,
  jobId,
  polling: isPolling,
  loading,
  error: pollingError,
  lastSyncedAt,
  start: startPolling,
  stop: stopPolling,
  refresh: refreshJob,
  reset: resetPolling,
} = useJobPolling()

function validateLength(raw: string, min: number, max: number, label: string): string {
  const value = raw.trim()
  if (value === '') return `${label}不能为空`
  if (value.length < min) return `${label}至少 ${min} 个字符，当前 ${value.length}`
  if (value.length > max) return `${label}最多 ${max} 个字符，当前 ${value.length}`
  return ''
}

const domainIssue = computed(() => validateLength(form.customer_domain, DOMAIN_MIN, DOMAIN_MAX, '客户领域'))
const descriptionIssue = computed(() =>
  validateLength(form.project_description, DESC_MIN, DESC_MAX, '项目描述'),
)

const showDomainIssue = computed(() => domainIssue.value !== '' && (touched.customer_domain || submitted.value))
const showDescriptionIssue = computed(
  () => descriptionIssue.value !== '' && (touched.project_description || submitted.value),
)

const canSubmit = computed(() => domainIssue.value === '' && descriptionIssue.value === '' && !submitting.value)

/** 提交错误优先展示，其次是在途轮询的错误 */
const displayError = computed(() => submitError.value || pollingError.value)

const currentStatus = computed(
  // 首拉回来之前按 PENDING 展示：CT-001 保证 202 时作业已以 PENDING 落库
  () => job.value?.status ?? 'PENDING',
)

const lastSyncedLabel = computed(() => {
  const at = lastSyncedAt.value
  return at === null ? '' : `最近同步 ${at.toLocaleTimeString('zh-CN', { hour12: false })}`
})

const resultView = computed(() => buildResultView(job.value?.result))
/** ERROR 时 result 是错误摘要字符串，COMPLETE 时才是结构化对象（CT-002） */
const resultHeading = computed(() => (currentStatus.value.toUpperCase() === 'ERROR' ? '错误摘要' : '执行结果'))
// 拆成三个扁平 computed，避免模板里对联合类型做窄化
const resultBlocks = computed(() => (resultView.value.kind === 'blocks' ? resultView.value.blocks : []))
const resultText = computed(() => (resultView.value.kind === 'text' ? resultView.value.text : ''))
const resultRaw = computed(() => (resultView.value.kind === 'raw' ? resultView.value.text : ''))

/**
 * 事件流水：接口按 timestamp 正序返回（最早在前），界面**倒序**展示，
 * 最新的一条排在最上面——这一点在标题里写清楚（REQ-012）。
 */
const events = computed(() => [...(job.value?.events ?? [])].reverse())

function formatEventTime(timestamp: string): string {
  const date = new Date(timestamp)
  if (Number.isNaN(date.getTime())) return timestamp
  return date.toLocaleString('zh-CN', { hour12: false })
}

async function handleSubmit(): Promise<void> {
  submitted.value = true
  if (!canSubmit.value || submitting.value) return

  submitting.value = true
  submitError.value = ''
  copyHint.value = ''
  try {
    const id = await submitCrew({
      customer_domain: form.customer_domain.trim(),
      project_description: form.project_description.trim(),
    })
    lookupId.value = id
    // start() 内部会立刻拉一次，并因 jobId 变化把 job_id 同步进 URL
    startPolling(id)
  } catch (cause) {
    submitError.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    submitting.value = false
  }
}

/** 按 job_id 查询历史作业（刷新后回填时也走这里） */
function handleLookup(): void {
  const id = lookupId.value.trim()
  if (id === '') return
  submitError.value = ''
  startPolling(id)
}

function handleRefresh(): void {
  void refreshJob()
}

/** 放弃当前作业：停止轮询、清空结果；jobId 变空会把 URL 里的 job_id 一并移除 */
function handleClearJob(): void {
  submitError.value = ''
  copyHint.value = ''
  resetPolling()
}

async function copyShareLink(): Promise<void> {
  copyHint.value = ''
  if (typeof navigator === 'undefined' || navigator.clipboard === undefined) {
    copyHint.value = '当前环境不支持自动复制，请手动复制地址栏'
    return
  }
  try {
    await navigator.clipboard.writeText(window.location.href)
    copyHint.value = '分享链接已复制'
  } catch {
    copyHint.value = '复制失败，请手动复制地址栏'
  }
}

function useSample(): void {
  form.customer_domain = SAMPLE.customer_domain
  form.project_description = SAMPLE.project_description
  submitted.value = false
}

function clearForm(): void {
  form.customer_domain = ''
  form.project_description = ''
  submitted.value = false
}

watch(jobId, (id) => writeJobIdToUrl(id))

onMounted(() => {
  // 带着 ?job_id= 打开（刷新 / 分享链接）时自动回填并继续查询
  const restored = readJobIdFromUrl()
  if (restored !== '') {
    lookupId.value = restored
    startPolling(restored)
  }
})

// useJobPolling 内部已用 onScopeDispose 兜底，这里再显式停一次，避免计时器泄漏
onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="page">
    <header class="page-header">
      <h1>营销内容生成控制台</h1>
      <p class="subtitle">
        CrewAI Flow 多智能体：市场调研 → 内容创作。提交后由 Worker 异步执行，页面每 3 秒轮询一次状态，
        到达终态（已完成 / 失败）自动停止。
      </p>
    </header>

    <div class="layout">
      <!-- 左栏：提交表单 + 历史查询 -->
      <section class="panel">
        <h2>新建作业</h2>
        <form novalidate @submit.prevent="handleSubmit">
          <div class="field">
            <label for="customer_domain">客户领域 / 官网</label>
            <input
              id="customer_domain"
              v-model="form.customer_domain"
              type="text"
              autocomplete="off"
              placeholder="openai.com"
              :aria-invalid="showDomainIssue"
              :aria-describedby="'customer_domain_hint'"
              @blur="touched.customer_domain = true"
            />
            <p id="customer_domain_hint" class="hint" :class="{ 'hint-invalid': showDomainIssue }">
              <span v-if="showDomainIssue">{{ domainIssue }}</span>
              <span v-else>3–255 个字符（去除首尾空格后计）</span>
              <span class="counter">{{ form.customer_domain.trim().length }} / {{ DOMAIN_MAX }}</span>
            </p>
          </div>

          <div class="field">
            <label for="project_description">项目描述</label>
            <textarea
              id="project_description"
              v-model="form.project_description"
              rows="8"
              placeholder="项目背景、目标受众、希望达成的营销目标……"
              :aria-invalid="showDescriptionIssue"
              :aria-describedby="'project_description_hint'"
              @blur="touched.project_description = true"
            />
            <p id="project_description_hint" class="hint" :class="{ 'hint-invalid': showDescriptionIssue }">
              <span v-if="showDescriptionIssue">{{ descriptionIssue }}</span>
              <span v-else>10–4000 个字符（去除首尾空格后计）</span>
              <span class="counter">{{ form.project_description.trim().length }} / {{ DESC_MAX }}</span>
            </p>
          </div>

          <div class="actions">
            <button type="submit" class="primary" :disabled="!canSubmit">
              {{ submitting ? '提交中…' : '提交作业' }}
            </button>
            <button type="button" :disabled="submitting" @click="useSample">填充示例</button>
            <button type="button" :disabled="submitting" @click="clearForm">清空</button>
          </div>
        </form>

        <hr />

        <h2>查询历史作业</h2>
        <p class="hint">作业存在后端，凭 job_id 随时可查；当前 job_id 也会同步到地址栏，链接可直接分享。</p>
        <div class="lookup">
          <input
            v-model="lookupId"
            type="text"
            autocomplete="off"
            placeholder="粘贴 job_id"
            aria-label="job_id"
            @keyup.enter="handleLookup"
          />
          <button type="button" :disabled="lookupId.trim() === '' || loading" @click="handleLookup">
            查询
          </button>
        </div>

        <p v-if="displayError" class="alert" role="alert">{{ displayError }}</p>
      </section>

      <!-- 右栏：状态 / 结果 / 事件 -->
      <section class="panel">
        <div class="panel-head">
          <h2>作业状态</h2>
          <button v-if="jobId !== ''" type="button" class="ghost" :disabled="loading" @click="handleRefresh">
            立即刷新
          </button>
        </div>

        <p v-if="jobId === ''" class="empty">提交作业或按 job_id 查询后，这里显示执行状态。</p>

        <template v-else>
          <div class="job-meta">
            <StatusBadge :status="currentStatus" />
            <span class="muted">{{ isPolling ? '轮询中（每 3 秒）' : '已停止轮询' }}</span>
            <span v-if="lastSyncedLabel" class="muted">{{ lastSyncedLabel }}</span>
          </div>

          <div class="job-id">
            <code>{{ jobId }}</code>
            <button type="button" class="ghost" @click="copyShareLink">复制分享链接</button>
            <button type="button" class="ghost" @click="handleClearJob">清除</button>
          </div>
          <p v-if="copyHint" class="hint">{{ copyHint }}</p>

          <template v-if="resultView.kind !== 'none'">
            <h3>{{ resultHeading }}</h3>

            <div v-if="resultBlocks.length > 0" class="blocks">
              <article v-for="block in resultBlocks" :key="block.key" class="block">
                <h4>{{ block.title }}</h4>
                <pre v-if="block.text !== ''" class="pre">{{ block.text }}</pre>
                <dl v-if="block.fields.length > 0" class="fields">
                  <template v-for="field in block.fields" :key="field.label">
                    <dt>{{ field.label }}</dt>
                    <dd>{{ field.value }}</dd>
                  </template>
                </dl>
                <div v-for="group in block.groups" :key="group.label" class="group">
                  <p class="group-label">{{ group.label }}</p>
                  <ul>
                    <li v-for="(item, index) in group.items" :key="`${group.label}-${index}`">{{ item }}</li>
                  </ul>
                </div>
              </article>
            </div>

            <pre v-else-if="resultText !== ''" class="pre">{{ resultText }}</pre>
            <pre v-else class="pre">{{ resultRaw }}</pre>
          </template>
          <p v-else class="empty">作业尚未产生结果（排队中或执行中）。</p>

          <h3>事件流水（共 {{ events.length }} 条，最新在最上面）</h3>
          <ol v-if="events.length > 0" class="events">
            <li v-for="(event, index) in events" :key="`${event.timestamp}-${index}`">
              <time :datetime="event.timestamp" :title="event.timestamp">
                {{ formatEventTime(event.timestamp) }}
              </time>
              <pre class="pre">{{ event.data }}</pre>
            </li>
          </ol>
          <p v-else class="empty">暂无事件，Flow 可能仍在排队。</p>
        </template>
      </section>
    </div>
  </div>
</template>

<style scoped>
.page {
  max-width: 1180px;
  margin: 0 auto;
}

.page-header h1 {
  font-size: 1.7rem;
  margin: 0;
}

.subtitle {
  margin-top: 0.35rem;
  color: var(--color-text-mute);
  font-size: 0.9rem;
}

.layout {
  margin-top: 1.5rem;
  display: grid;
  grid-template-columns: minmax(320px, 420px) 1fr;
  gap: 1.25rem;
  align-items: start;
}

@media (max-width: 900px) {
  .layout {
    grid-template-columns: 1fr;
  }
}

h2 {
  font-size: 1.05rem;
  margin: 0 0 0.9rem;
}

h3 {
  font-size: 0.95rem;
  margin: 1.2rem 0 0.5rem;
}

h4 {
  font-size: 0.88rem;
  margin: 0 0 0.4rem;
  color: var(--color-heading);
}

.field {
  margin-bottom: 0.9rem;
}

label {
  display: block;
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--color-heading);
  margin-bottom: 0.35rem;
}

.hint {
  display: flex;
  justify-content: space-between;
  gap: 0.75rem;
  margin: 0.3rem 0 0;
  font-size: 0.78rem;
  color: var(--color-text-mute);
}

.hint-invalid {
  color: var(--alert-border);
}

.counter {
  font-variant-numeric: tabular-nums;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}

hr {
  border: none;
  border-top: 1px solid var(--color-border);
  margin: 1.25rem 0;
}

.lookup {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.6rem;
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}

.panel-head h2 {
  margin-bottom: 0;
}

.job-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.7rem;
  margin-top: 0.9rem;
}

.job-id {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-top: 0.6rem;
}

.job-id code {
  font-size: 0.82rem;
  word-break: break-all;
}

.muted {
  color: var(--color-text-mute);
  font-size: 0.78rem;
}

.empty {
  margin-top: 0.85rem;
  color: var(--color-text-mute);
  font-size: 0.85rem;
}

.blocks {
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
}

.block {
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 0.8rem 0.9rem;
  background: var(--color-surface);
}

.pre {
  margin: 0;
  padding: 0.7rem 0.8rem;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-background);
  font-size: 0.8rem;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 24rem;
  overflow: auto;
}

.block .pre {
  background: var(--color-surface-mute);
}

.fields {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 0.25rem 0.8rem;
  margin: 0.6rem 0 0;
  font-size: 0.85rem;
}

.fields dt {
  color: var(--color-text-mute);
}

.fields dd {
  margin: 0;
  word-break: break-word;
}

.group {
  margin-top: 0.6rem;
  font-size: 0.85rem;
}

.group-label {
  margin: 0 0 0.2rem;
  color: var(--color-text-mute);
}

.group ul {
  margin: 0;
  padding-left: 1.1rem;
}

.events {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  max-height: 26rem;
  overflow: auto;
}

.events time {
  display: block;
  font-size: 0.75rem;
  color: var(--color-text-mute);
  margin-bottom: 0.25rem;
}
</style>
