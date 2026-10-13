<script setup lang="ts">
/**
 * Заглушка раздела, который наполняется на следующем этапе (§12).
 *
 * Показывает, что именно здесь появится по спецификации, — чтобы каркас Э1 был
 * честным: страница существует, роут работает, но данные придут позже.
 */
defineProps<{
  title: string
  stage: string
  description: string
  bullets?: string[]
}>()
</script>

<template>
  <section class="space-y-4">
    <div class="rounded-xl border border-amber-200 bg-amber-50 p-5">
      <div class="flex items-start gap-3">
        <i class="pi pi-info-circle mt-0.5 text-amber-600" />
        <div>
          <p class="font-semibold text-amber-900">
            {{ title }} — раздел появится на этапе {{ stage }}
          </p>
          <p class="mt-1 text-sm text-amber-900/80">{{ description }}</p>
        </div>
      </div>
    </div>

    <ul v-if="bullets?.length" class="space-y-2 rounded-xl border border-slate-200 bg-white p-5">
      <li v-for="item in bullets" :key="item" class="flex items-start gap-2 text-sm text-slate-700">
        <i class="pi pi-check mt-1 text-[10px] text-emerald-500" />
        <span>{{ item }}</span>
      </li>
    </ul>

    <slot />
  </section>
</template>
