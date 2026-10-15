<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ status: string }>()

/**
 * 四个已知状态的中文标签（spec/020 §1.1）。
 * 未知状态原样展示——CT-002 约定新增状态不是破坏性变更，前端不能把新状态吞成空白。
 */
const LABELS: Record<string, string> = {
  PENDING: '排队中',
  RUNNING: '运行中',
  COMPLETE: '已完成',
  ERROR: '失败',
}

const code = computed(() => String(props.status ?? '').toUpperCase())
const label = computed(() => LABELS[code.value] ?? String(props.status ?? '未知'))
const tone = computed(() => (code.value in LABELS ? code.value.toLowerCase() : 'unknown'))
</script>

<template>
  <span class="badge" :class="tone" role="status">
    <span class="dot" aria-hidden="true" />
    <span class="label">{{ label }}</span>
  </span>
</template>

<style scoped>
.badge {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  padding: 0.25rem 0.7rem;
  border: 1px solid currentColor;
  border-radius: 999px;
  background: color-mix(in srgb, currentColor 12%, transparent);
  font-size: 0.82rem;
  font-weight: 600;
  white-space: nowrap;
}

.dot {
  width: 0.5rem;
  height: 0.5rem;
  border-radius: 50%;
  background: currentColor;
}

/* 颜色全部走全局变量，深色模式由 main.css 的 prefers-color-scheme 统一接管 */
.pending {
  color: var(--tone-pending);
}

.running {
  color: var(--tone-running);
}

.complete {
  color: var(--tone-complete);
}

.error {
  color: var(--tone-error);
}

.unknown {
  color: var(--tone-unknown);
}

/* 运行中：呼吸动效，提示作业正在后台异步执行 */
.running .dot {
  animation: badge-breath 1.6s ease-in-out infinite;
}

@keyframes badge-breath {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.3;
    transform: scale(0.72);
  }
}

@media (prefers-reduced-motion: reduce) {
  .running .dot {
    animation: none;
  }
}
</style>
