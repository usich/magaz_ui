<script setup lang="ts">
/**
 * Компактная панель операций по магазинам — в стиле `LocalOperationPanel`
 * вкладки «Локальные базы»: первая полоска — «Проверить файлы / Слияние /
 * Обновить базу», вторая — «Отправить сообщение / Разослать ссылку».
 *
 * Принимает выбранные магазины (`storeIds`) либо «все», и для каждого действия
 * вызывает `dispatchAction` (магазинный `POST /stores/{id}/actions` или
 * `POST /bulk/actions`). Итог — в истории задач и статусах таблицы.
 */
import { computed, reactive, ref } from 'vue'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import InputText from 'primevue/inputtext'
import RadioButton from 'primevue/radiobutton'
import { useToast } from 'primevue/usetoast'

import { dispatchAction, type ActionOutcome } from '@/api/actions'
import type { ActionName, StoreItem } from '@/api/types'

const props = withDefaults(
  defineProps<{
    storeIds?: number[]
    /** Нет выбора — операция идёт по всем активным магазинам (`allStores`). */
    all?: boolean
    allStores?: StoreItem[]
    title?: string
  }>(),
  { storeIds: () => [], all: false, allStores: () => [], title: 'Массовые операции по магазинам' },
)

const emit = defineEmits<{ submitted: [] }>()

const toast = useToast()

const check = reactive({ filename: 'update' })
const merge = reactive({ file_name: 'update', is_extension: false, extension_name: '' })
const update = reactive({ is_extension: false, extension_name: '', is_fast_update: false })
const message = reactive({ text_message: '' })
const link = reactive({ url: '' })

const busy = ref<null | ActionName>(null)

/** Получатели операции: выбранные магазины, либо все активные. */
const targets = computed(() => {
  if (props.storeIds.length) return props.storeIds
  return props.allStores.filter((item) => item.is_active).map((item) => item.id)
})

const targetLabel = computed(() =>
  props.all || !props.storeIds.length
    ? 'все активные магазины'
    : `выбрано магазинов: ${props.storeIds.length}`,
)

function notifyError(error: unknown, fallback: string): void {
  const detail = error instanceof Error ? error.message : fallback
  toast.add({ severity: 'error', summary: 'Операция не выполнена', detail, life: 6000 })
}

async function run(action: ActionName, params: Record<string, unknown>, summary: string): Promise<void> {
  const ids = targets.value
  if (!ids.length) return
  busy.value = action
  try {
    const outcome: ActionOutcome = await dispatchAction({ storeIds: ids }, action, params)
    toast.add({
      severity: outcome.warning ? 'warn' : 'success',
      summary,
      detail: outcome.warning ?? outcome.message ?? 'Задачи поставлены в очередь',
      life: 6000,
    })
    emit('submitted')
  } catch (error) {
    notifyError(error, 'Не удалось поставить операцию')
  } finally {
    busy.value = null
  }
}
</script>

<template>
  <section class="rounded-xl border border-slate-200 bg-white p-5" data-test="store-operation-strip">
    <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h2 class="font-semibold">{{ props.title }}</h2>
      <span class="magaz-muted text-sm">{{ targetLabel }}</span>
    </div>

    <div class="grid gap-4 md:grid-cols-3">
      <!-- Проверить файлы -->
      <div class="rounded-lg border border-slate-200 p-4" data-test="store-check-form">
        <h4 class="text-sm font-semibold">Проверить файлы</h4>
        <label class="mt-2 block space-y-1">
          <span class="magaz-muted text-xs">Имя файла (без расширения)</span>
          <InputText v-model="check.filename" class="w-full" placeholder="update" data-test="store-check-filename" />
        </label>
        <Button label="Проверить" icon="pi pi-search" size="small" severity="secondary" class="mt-3"
          :loading="busy === 'check_files'" :disabled="busy !== null || !check.filename.trim()"
          data-test="store-run-check"
          @click="run('check_files', { filename: check.filename.trim() }, 'Проверка файлов поставлена в очередь')" />
      </div>

      <!-- Слияние -->
      <div class="rounded-lg border border-slate-200 p-4" data-test="store-merge-form">
        <h4 class="text-sm font-semibold">Слияние</h4>
        <label class="mt-2 block space-y-1">
          <span class="magaz-muted text-xs">Имя файла (без расширения)</span>
          <InputText v-model="merge.file_name" class="w-full" placeholder="update" data-test="store-merge-file" />
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
          <InputText v-model="merge.extension_name" class="w-full" placeholder="БГ_Торговоя" data-test="store-merge-extension" />
        </label>
        <Button label="Запустить слияние" size="small" class="mt-3" :loading="busy === 'run_merge'"
          :disabled="busy !== null || !merge.file_name.trim()" data-test="store-run-merge"
          @click="run('run_merge', { file_name: merge.file_name.trim(), is_extension: merge.is_extension, ...(merge.is_extension && merge.extension_name.trim() ? { extension_name: merge.extension_name.trim() } : {}) }, 'Слияние поставлено в очередь')" />
      </div>

      <!-- Обновить базу -->
      <div class="rounded-lg border border-slate-200 p-4" data-test="store-update-form">
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
          <InputText v-model="update.extension_name" class="w-full" placeholder="БГ_Торговоя" data-test="store-update-extension" />
        </label>
        <label class="mt-2 flex items-center gap-2 text-sm">
          <Checkbox v-model="update.is_fast_update" binary />
          <span>Быстрое обновление (без завершения сеансов)</span>
        </label>
        <Button label="Обновить" size="small" class="mt-3" :loading="busy === 'run_update_db'"
          :disabled="busy !== null" data-test="store-run-update"
          @click="run('run_update_db', { is_fast_update: update.is_fast_update, is_extension: update.is_extension, ...(update.is_extension && update.extension_name.trim() ? { extension_name: update.extension_name.trim() } : {}) }, 'Обновление БД поставлено в очередь')" />
      </div>
    </div>


    <div class="mt-4 grid gap-4 md:grid-cols-2">
      <!-- Отправить сообщение -->
      <div class="rounded-lg border border-slate-200 p-4" data-test="store-message-form">
        <h4 class="text-sm font-semibold">Отправить сообщение</h4>
        <label class="mt-2 block space-y-1">
          <span class="magaz-muted text-xs">Текст окна</span>
          <InputText v-model="message.text_message" class="w-full" placeholder="База будет обновлена через 5 минут" data-test="store-message-text" />
        </label>
        <p class="text-xs text-amber-700">Команда завершит <span class="magaz-mono">1cv8.exe</span> у всех пользователей.</p>
        <Button label="Отправить сообщение" size="small" severity="warn" class="mt-3"
          :loading="busy === 'send_message'" :disabled="busy !== null || !message.text_message.trim()"
          data-test="store-run-message"
          @click="run('send_message', { text_message: message.text_message.trim() }, 'Сообщение отправлено')" />
      </div>

      <!-- Разослать ссылку -->
      <div class="rounded-lg border border-slate-200 p-4" data-test="store-link-form">
        <h4 class="text-sm font-semibold">Разослать ссылку на пакет</h4>
        <label class="mt-2 block space-y-1">
          <span class="magaz-muted text-xs">Ссылка Яндекс.Диска</span>
          <InputText v-model="link.url" class="w-full" placeholder="https://disk.yandex.ru/d/XXXX" data-test="store-link-url" />
        </label>
        <Button label="Скачать на магазинах" size="small" class="mt-3" :loading="busy === 'download_file'"
          :disabled="busy !== null || !link.url.trim()" data-test="store-run-link"
          @click="run('download_file', { 'url-file': link.url.trim() }, 'Скачивание поставлено в очередь')" />
      </div>
    </div>
  </section>
</template>
