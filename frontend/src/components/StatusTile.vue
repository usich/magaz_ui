<script setup lang="ts">
/**
 * Плитка-счётчик на главной (§11.1): «Всего магазинов», «Онлайн», «Офлайн» и т.д.
 */
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    label: string
    value: number | string
    icon?: string
    hint?: string
    tone?: 'neutral' | 'positive' | 'negative' | 'muted'
  }>(),
  { icon: 'pi pi-chart-bar', hint: '', tone: 'neutral' },
)

const toneClasses = computed(() => {
  switch (props.tone) {
    case 'positive':
      return 'text-emerald-600'
    case 'negative':
      return 'text-rose-600'
    case 'muted':
      return 'text-slate-400'
    default:
      return 'text-slate-900'
  }
})
</script>

<template>
  <div class="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
    <div class="flex items-start justify-between">
      <p class="magaz-muted text-xs font-medium uppercase tracking-wide">{{ label }}</p>
      <i :class="[icon, 'text-slate-300']" />
    </div>
    <p :class="['mt-2 text-3xl font-semibold', toneClasses]" data-test="tile-value">{{ value }}</p>
    <p v-if="hint" class="magaz-muted mt-1 text-xs">{{ hint }}</p>
  </div>
</template>
