<script setup lang="ts">
/** Раздел «Задачи» (§11.6) — наполняется на этапе Э4. */
import StagePlaceholder from '@/components/StagePlaceholder.vue'
</script>

<template>
  <StagePlaceholder
    title="История задач"
    stage="Э4"
    description="Все операции по магазинам с фильтрами и полным логом."
    :bullets="[
      'Фильтры по магазину, типу операции, статусу и датам',
      'Статусы: queued / sent / running / success / failed / timeout / error / canceled',
      'Полный лог задачи (job_logs) и ссылки на связанные уведомления',
    ]"
  />
</template>
