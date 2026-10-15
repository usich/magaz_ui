<script setup lang="ts">
/**
 * Подтверждение опасного действия (Э10).
 *
 * Своего сервиса подтверждений в приложении нет: удаление объектов — первая
 * операция «насовсем», поэтому диалог коротко перечисляет, что исчезнет вместе
 * с объектом, и что останется (например, уже показанные окна на кассах, §7.4).
 */
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'

defineProps<{
  visible: boolean
  header: string
  message: string
  /** Что именно уйдёт вместе с объектом — короткие строки списком. */
  details?: string[]
  acceptLabel?: string
  loading?: boolean
}>()

const emit = defineEmits<{
  (event: 'update:visible', value: boolean): void
  (event: 'confirm'): void
}>()

function close(): void {
  emit('update:visible', false)
}
</script>

<template>
  <Dialog
    :visible="visible"
    modal
    :header="header"
    :style="{ width: '30rem' }"
    data-test="confirm-dialog"
    @update:visible="emit('update:visible', $event)"
  >
    <p class="text-sm">{{ message }}</p>
    <ul v-if="details && details.length" class="magaz-muted mt-3 list-disc space-y-1 pl-5 text-xs">
      <li v-for="item in details" :key="item">{{ item }}</li>
    </ul>
    <template #footer>
      <Button
        label="Отмена"
        size="small"
        text
        data-test="confirm-cancel"
        @click="close"
      />
      <Button
        :label="acceptLabel ?? 'Удалить'"
        size="small"
        severity="danger"
        :loading="loading"
        data-test="confirm-accept"
        @click="emit('confirm')"
      />
    </template>
  </Dialog>
</template>
