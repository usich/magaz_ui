<script setup lang="ts">
/** Отчёт по уведомлению (§11.5) — наполняется на этапе Э5. */
import StagePlaceholder from '@/components/StagePlaceholder.vue'

defineProps<{ id: string }>()
</script>

<template>
  <StagePlaceholder
    title="Отчёт по уведомлению"
    stage="Э5"
    :description="`Таблица по магазинам и сессиям для уведомления id=${id}: кто ознакомлен, кто отложил, кто ещё ждёт.`"
    :bullets="[
      'Строка на каждую сессию Windows: пользователь, статус, время показа, время ознакомления',
      'Отложенные выделяются отдельно: они не считаются ознакомленными (П-2)',
      'Экспорт CSV для отчётности',
    ]"
  />
</template>
