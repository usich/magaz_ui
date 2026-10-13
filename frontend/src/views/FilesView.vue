<script setup lang="ts">
/** Раздел «Файлы» (§11.3) — наполняется на этапе Э4. */
import StagePlaceholder from '@/components/StagePlaceholder.vue'
</script>

<template>
  <StagePlaceholder
    title="Файлы обновления"
    stage="Э4"
    description="Матрица «магазин × (cf/cfe/xml)» с размерами и датами последней проверки."
    :bullets="[
      'Кнопка «Проверить на всех» (или на выбранных) с полем «имя файла»',
      'Красная подсветка магазинов, где нужного файла нет, счётчик «файл есть у N из M»',
      'Кнопка «Разослать ссылку» (ФН2): URL → POST /download-file → повторная проверка',
    ]"
  />
</template>
