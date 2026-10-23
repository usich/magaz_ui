<script setup lang="ts">
/**
 * Панель массовых операций по локальным базам — зеркало `OperationPanel`
 * вкладки «Магазины» (Д-26): проверка файлов, слияние, обновление БД.
 *
 * Принимает выбранные базы (`localBaseIds`) либо «все» (когда ничего не выбрано),
 * и для каждого действия вызывает `POST /local-bases/bulk/update` с нужным `kind`.
 * Итог операции — в истории задач (`/jobs`) и статусах в таблице.
 */
import { computed, reactive, ref } from 'vue'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import InputText from 'primevue/inputtext'
import RadioButton from 'primevue/radiobutton'
import { useToast } from 'primevue/usetoast'

import { ApiError } from '@/api/client'
import { localBasesApi } from '@/api/endpoints'
import type { LocalBaseItem } from '@/api/types'

const props = withDefaults(
  defineProps<{
    localBaseIds?: number[]
    /** Нет выбора — операция идёт по всем активным базам (`allBases`). */
    all?: boolean
    allBases?: LocalBaseItem[]
    title?: string
  }>(),
  { localBaseIds: () => [], all: false, allBases: () => [], title: 'Массовые операции по локальным базам' },
)

const emit = defineEmits<{ submitted: [] }>()

const toast = useToast()

const check = reactive({ file_name: 'update' })
const merge = reactive({ file_name: 'update', is_extension: false, extension_name: '' })
const update = reactive({ is_extension: false, extension_name: '', is_fast_update: false })

const busy = ref<null | 'check' | 'merge' | 'update'>(null)

/** Получатели операции: выбранные базы, либо все активные. */
const targets = computed(() => {
  if (props.localBaseIds.length) return props.localBaseIds
  return props.allBases.filter((item) => item.is_active).map((item) => item.id)
})

const targetLabel = computed(() =>
  props.all || !props.localBaseIds.length
    ? 'все активные базы'
    : `выбрано баз: ${props.localBaseIds.length}`,
)

function notifyError(error: unknown, fallback: string): void {
  const detail =
    error instanceof ApiError && error.messages.length > 0
      ? error.messages.join('; ')
      : error instanceof ApiError
        ? error.message
        : fallback
  toast.add({ severity: 'error', summary: 'Операция не выполнена', detail, life: 6000 })
}

async function run(
  kind: 'check_files_local' | 'run_merge_local' | 'run_update_db_local',
  payload: Record<string, unknown>,
  summary: string,
): Promise<void> {
  const ids = targets.value
  if (!ids.length) return
  busy.value = kind === 'check_files_local' ? 'check' : kind === 'run_merge_local' ? 'merge' : 'update'
  try {
    const response = await localBasesApi.bulkUpdate({ local_base_ids: ids, kind, ...payload })
    toast.add({ severity: 'success', summary, detail: response.message, life: 5000 })
    emit('submitted')
  } catch (error) {
    notifyError(error, 'Не удалось поставить операцию')
  } finally {
    busy.value = null
  }
}
</script>

<template>
  <section class="rounded-xl border border-slate-200 bg-white p-5" data-test="local-operation-panel">
    <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h2 class="font-semibold">{{ props.title }}</h2>
      <span class="magaz-muted text-sm">{{ targetLabel }}</span>
    </div>

    <div class="grid gap-4 md:grid-cols-3">
      <!-- Проверить файлы -->
      <div class="rounded-lg border border-slate-200 p-4" data-test="local-check-form">
        <h4 class="text-sm font-semibold">Проверить файлы</h4>
        <label class="mt-2 block space-y-1">
          <span class="magaz-muted text-xs">Имя файла (без расширения)</span>
          <InputText v-model="check.file_name" class="w-full" placeholder="update" data-test="local-check-filename" />
        </label>
        <Button
          label="Проверить"
          icon="pi pi-search"
          size="small"
          severity="secondary"
          class="mt-3"
          :loading="busy === 'check'"
          :disabled="busy !== null || !check.file_name.trim()"
          data-test="local-run-check"
          @click="run('check_files_local', { file_name: check.file_name.trim() }, 'Проверка файлов поставлена в очередь')"
        />
      </div>

      <!-- Слияние -->
      <div class="rounded-lg border border-slate-200 p-4" data-test="local-merge-form">
        <h4 class="text-sm font-semibold">Слияние</h4>
        <label class="mt-2 block space-y-1">
          <span class="magaz-muted text-xs">Имя файла (без расширения)</span>
          <InputText v-model="merge.file_name" class="w-full" placeholder="update" data-test="local-merge-file" />
        </label>
        <div class="mt-2 flex items-center gap-4 text-sm">
          <label class="flex items-center gap-1">
            <RadioButton v-model="merge.is_extension" :value="false" />
            <span>Основная (.cf)</span>
          </label>
          <label class="flex items-center gap-1">
            <RadioButton v-model="merge.is_extension" :value="true" />
            <span>Расширение (.cfe)</span>
          </label>
        </div>
        <label v-if="merge.is_extension" class="mt-2 block space-y-1">
          <span class="magaz-muted text-xs">Имя расширения</span>
          <InputText v-model="merge.extension_name" class="w-full" placeholder="БГ_Бухгалтерия" data-test="local-merge-extension" />
        </label>
        <Button
          label="Запустить слияние"
          size="small"
          class="mt-3"
          :loading="busy === 'merge'"
          :disabled="busy !== null || !merge.file_name.trim()"
          data-test="local-run-merge"
          @click="run('run_merge_local', { file_name: merge.file_name.trim(), is_extension: merge.is_extension, ...(merge.is_extension && merge.extension_name.trim() ? { extension_name: merge.extension_name.trim() } : {}) }, 'Слияние поставлено в очередь')"
        />
      </div>

      <!-- Обновить базу -->
      <div class="rounded-lg border border-slate-200 p-4" data-test="local-update-form">
        <h4 class="text-sm font-semibold">Обновить базу</h4>
        <div class="mt-2 flex items-center gap-4 text-sm">
          <span class="magaz-muted text-xs">Обновляем:</span>
          <label class="flex items-center gap-1">
            <RadioButton v-model="update.is_extension" :value="false" />
            <span>Основная (.cf)</span>
          </label>
          <label class="flex items-center gap-1">
            <RadioButton v-model="update.is_extension" :value="true" />
            <span>Расширение (.cfe)</span>
          </label>
        </div>
        <label v-if="update.is_extension" class="mt-2 block space-y-1">
          <span class="magaz-muted text-xs">Имя расширения</span>
          <InputText v-model="update.extension_name" class="w-full" placeholder="БГ_Бухгалтерия" data-test="local-update-extension" />
        </label>
        <label class="mt-2 flex items-center gap-2 text-sm">
          <Checkbox v-model="update.is_fast_update" binary />
          <span>Быстрое обновление (без завершения сеансов)</span>
        </label>
        <Button
          label="Обновить"
          size="small"
          class="mt-3"
          :loading="busy === 'update'"
          :disabled="busy !== null"
          data-test="local-run-update"
          @click="run('run_update_db_local', { is_fast_update: update.is_fast_update, is_extension: update.is_extension, ...(update.is_extension && update.extension_name.trim() ? { extension_name: update.extension_name.trim() } : {}) }, 'Обновление поставлено в очередь')"
        />
      </div>
    </div>
  </section>
</template>
