<script setup lang="ts">
/**
 * Диалог обновления локальной базы 1С (Д-24): выбор файла `.cf/.cfe`, режим.
 *
 * По «Запустить» ставит задачу `run_update_db_local` через
 * `POST /admin/local-bases/{id}/run-update`. Сервер отвечает `202` и задачу
 * выполняет LocalUpdateMonitor напрямую на этой машине. Повторный запуск, пока
 * задача активна, приходит как `409` — показываем текст из ответа (R12).
 */
import { computed, reactive, ref, watch } from 'vue'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import RadioButton from 'primevue/radiobutton'
import { useToast } from 'primevue/usetoast'

import { ApiError } from '@/api/client'
import { localBasesApi } from '@/api/endpoints'
import type { LocalBaseItem } from '@/api/types'

const props = withDefaults(
  defineProps<{
    visible: boolean
    /** База, которую обновляем (открывает диалог из строки таблицы). */
    base?: LocalBaseItem | null
    /** Массовый режим (Д-25): обновить эти базы одним запуском (без карточки `base`). */
    bulkBaseIds?: number[]
    /** Режим операции (Д-26): слияние `/MergeCfg` или обновление БД `/UpdateDBCfg`. */
    mode?: 'merge' | 'update'
  }>(),
  { base: null, bulkBaseIds: () => [], mode: 'update' },
)

const emit = defineEmits<{
  'update:visible': [value: boolean]
  /** Задача (или пачка задач) поставлена в очередь — родитель перечитывает статусы. */
  submitted: []
}>()

const toast = useToast()

const form = reactive({
  file_name: '',
  is_extension: false,
  extension_name: '',
  is_fast_update: false,
})

const isRunning = ref(false)
const formError = ref('')

const isBulk = computed(() => props.bulkBaseIds.length > 0)
const isMerge = computed(() => props.mode === 'merge')
const dialogTitle = computed(() => {
  if (isBulk.value) return `Обновление баз (выбрано: ${props.bulkBaseIds.length})`
  if (isMerge.value) return `Слияние базы «${props.base?.name ?? ''}»`
  return `Обновление базы «${props.base?.name ?? ''}»`
})
const isFormValid = computed(() => Boolean(form.file_name.trim()))

function reset(): void {
  // По умолчанию — обновление основной конфигурации (.cf), быстрое выключено.
  form.file_name = ''
  form.is_extension = false
  form.extension_name = props.base?.extension_name ?? ''
  form.is_fast_update = false
  formError.value = ''
}

watch(
  () => props.visible,
  (open) => {
    if (open) reset()
  },
)

function close(): void {
  emit('update:visible', false)
}

async function run(): Promise<void> {
  const canRunSingle = Boolean(props.base) && !isBulk.value
  const canRunBulk = isBulk.value && props.bulkBaseIds.length > 0
  if ((!canRunSingle && !canRunBulk) || !form.file_name.trim()) return
  isRunning.value = true
  formError.value = ''
  const payload = {
    file_name: form.file_name.trim(),
    is_fast_update: form.is_fast_update,
    is_extension: form.is_extension,
    ...(form.is_extension && form.extension_name.trim()
      ? { extension_name: form.extension_name.trim() }
      : {}),
  }
  try {
    if (isBulk.value) {
      const kind = isMerge.value ? 'run_merge_local' : 'run_update_db_local'
      const response = await localBasesApi.bulkUpdate({
        local_base_ids: props.bulkBaseIds,
        kind,
        ...(isMerge.value ? { full_merge: false } : {}),
        ...payload,
      })
      toast.add({
        severity: 'success',
        summary: isMerge.value ? 'Слияние поставлено в очередь' : 'Обновление поставлено в очередь',
        detail: response.message,
        life: 5000,
      })
    } else if (isMerge.value) {
      const response = await localBasesApi.runMerge(props.base!.id, payload)
      toast.add({
        severity: 'success',
        summary: 'Слияние поставлено в очередь',
        detail: response.message,
        life: 5000,
      })
    } else {
      const response = await localBasesApi.runUpdate(props.base!.id, payload)
      toast.add({
        severity: 'success',
        summary: 'Обновление поставлено в очередь',
        detail: response.message,
        life: 5000,
      })
    }
    emit('submitted')
    close()
  } catch (error) {
    const message =
      error instanceof ApiError && error.messages.length > 0
        ? error.messages.join('; ')
        : error instanceof ApiError
          ? error.message
          : 'Не удалось поставить обновление'
    formError.value = message
    toast.add({ severity: 'error', summary: 'Операция не выполнена', detail: message, life: 6000 })
  } finally {
    isRunning.value = false
  }
}
</script>

<template>
  <Dialog
    :visible="props.visible"
    :header="dialogTitle"
    modal
    :style="{ width: '480px' }"
    data-test="local-base-update-dialog"
    @update:visible="(v: boolean) => emit('update:visible', v)"
  >
    <div class="space-y-3">
      <label class="block space-y-1">
        <span class="magaz-muted text-xs">Имя файла обновления (без расширения или с ним)</span>
        <InputText v-model="form.file_name" class="w-full" placeholder="update" data-test="lbu-file" />
      </label>

      <div class="flex items-center gap-4 text-sm">
        <span class="magaz-muted text-xs">Обновляем:</span>
        <label class="flex items-center gap-1">
          <RadioButton v-model="form.is_extension" :value="false" data-test="lbu-cf" />
          <span>Основная (.cf)</span>
        </label>
        <label class="flex items-center gap-1">
          <RadioButton v-model="form.is_extension" :value="true" data-test="lbu-cfe" />
          <span>Расширение (.cfe)</span>
        </label>
      </div>

      <label v-if="form.is_extension" class="block space-y-1">
        <span class="magaz-muted text-xs">Имя расширения</span>
        <InputText v-model="form.extension_name" class="w-full" placeholder="БГ_Бухгалтерия" data-test="lbu-extension" />
      </label>

      <label v-if="!isMerge" class="flex items-center gap-2 text-sm">
        <Checkbox v-model="form.is_fast_update" binary />
        <span>Быстрое обновление (без завершения сеансов)</span>
      </label>

      <p v-if="formError" class="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700" data-test="lbu-error">
        {{ formError }}
      </p>
      <p class="magaz-muted text-xs">
        <template v-if="isBulk">
          Задачу получит каждая выбранная база; сколько из них выполняется параллельно —
          настраивается в «Настройках» (`local_update_concurrency`, по умолчанию 5).
          Базы, которые уже обновляются, будут пропущены.
        </template>
        <template v-else-if="isMerge">
          Слияние выполнит сервер напрямую на этой машине (файл `.cf/.cfe` из
          `local_update_dir`). Пока база занята операцией, повторный запуск для неё
          заблокирован.
        </template>
        <template v-else>
          Задачу выполнит сервер напрямую на этой машине. Пока база обновляется, повторный запуск
          для неё заблокирован.
        </template>
      </p>
    </div>

    <template #footer>
      <Button label="Отмена" text size="small" @click="close" />
      <Button
        :label="isMerge ? 'Запустить слияние' : 'Запустить'"
        size="small"
        :loading="isRunning"
        :disabled="!isFormValid"
        data-test="lbu-run"
        @click="run"
      />
    </template>
  </Dialog>
</template>
