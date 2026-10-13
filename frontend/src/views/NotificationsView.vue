<script setup lang="ts">
/** Раздел «Уведомления» (§11.5, ФН3) — наполняется на этапе Э5. */
import StagePlaceholder from '@/components/StagePlaceholder.vue'
</script>

<template>
  <StagePlaceholder
    title="Уведомления пользователям"
    stage="Э5"
    description="Создание уведомлений трёх уровней важности и отчёт «ознакомлены X из Y» по каждой сессии."
    :bullets="[
      'Адресация: все магазины / мультивыбор / по кластеру; флаги requires_ack, дедлайн, повтор, msg.exe',
      'Прогресс-бар и статус рассылки, отмена активного уведомления',
      'Кнопка «Отложить» (П-2): видно snooze_count и «отложено до», возврат через notification_snooze_min',
      'Экспорт отчёта в CSV',
    ]"
  />
</template>
