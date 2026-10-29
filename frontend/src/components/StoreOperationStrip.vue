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

const message = reactive({ text_message: '' })
const link = reactive({ url: '' })

const busy = ref<null | ActionName>(null)

/**
 * Единый блок «Файл обновления»: одно имя файла и один переключатель `.cf/.cfe`
 * на всю форму, чтобы не задавать их отдельно для проверки и для слияния.
 * Для `.cfe` используется `extension_name` (например `БГ_Торговоя`), для `.cf`
 * — `filename` (например `update`). Проверка файлов сканирует все типы по стему,
 * поэтому достаточно указать имя один раз.
 */
const fileForm = reactive({
  filename: 'update',
  is_extension: false,
  extension_name: '',
  is_fast_update: false,
})

/** Единое имя файла (стем) с учётом выбранного типа: `.cf` → `filename`, `.cfe` → `extension_name`. */
const effectiveFileName = computed(() => {
  const stem = fileForm.is_extension
    ? fileForm.extension_name.trim() || fileForm.filename.trim()
    : fileForm.filename.trim()
  return stem || 'update'
})

function mergeParams(): Record<string, unknown> {
  const file_name = effectiveFileName.value
  return {
    file_name,
    is_extension: fileForm.is_extension,
    ...(fileForm.is_extension && fileForm.extension_name.trim()
      ? { extension_name: fileForm.extension_name.trim() }
      : {}),
  }
}

function updateParams(): Record<string, unknown> {
  return {
    is_fast_update: fileForm.is_fast_update,
    is_extension: fileForm.is_extension,
    ...(fileForm.is_extension && fileForm.extension_name.trim()
      ? { extension_name: fileForm.extension_name.trim() }
      : {}),
  }
}

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
      <!-- Единый блок: файл обновления (имя + тип) -->
      <div class="rounded-lg border border-slate-200 p-4" data-test="store-file-form">
        <h4 class="text-sm font-semibold">Файл обновления</h4>
        <label class="mt-2 block space-y-1">
          <span class="magaz-muted text-xs">Имя файла (без расширения)</span>
          <InputText v-model="fileForm.filename" class="w-full" placeholder="update" data-test="store-file-name" />
        </label>
        <div class="mt-2 flex items-center gap-4 text-sm">
          <span class="magaz-muted text-xs">Тип файла:</span>
          <label class="flex items-center gap-1">
            <RadioButton v-model="fileForm.is_extension" :value="false" />
            <span>Основная (.cf)</span>
          </label>
          <label class="flex items-center gap-1">
            <RadioButton v-model="fileForm.is_extension" :value="true" />
            <span>Расширение (.cfe)</span>
          </label>
        </div>
        <label v-if="fileForm.is_extension" class="mt-2 block space-y-1">
          <span class="magaz-muted text-xs">Имя расширения</span>
          <InputText v-model="fileForm.extension_name" class="w-full" placeholder="БГ_Торговоя" data-test="store-file-extension" />
        </label>
        <label class="mt-2 flex items-center gap-2 text-sm">
          <Checkbox v-model="fileForm.is_fast_update" binary />
          <span>Быстрое обновление (без завершения сеансов)</span>
        </label>
        <Button label="Обновить базу" size="small" class="mt-3" :loading="busy === 'run_update_db'"
          :disabled="busy !== null" data-test="store-run-update"
          @click="run('run_update_db', updateParams(), 'Обновление БД поставлено в очередь')" />
      </div>

      <!-- Проверить файлы -->
      <div class="rounded-lg border border-slate-200 p-4" data-test="store-check-form">
        <h4 class="text-sm font-semibold">Проверить файлы</h4>
        <p class="mt-2 text-xs magaz-muted">Проверка файла «{{ effectiveFileName }}» (.cf/.cfe/.xml) на кассах.</p>
        <Button label="Проверить" icon="pi pi-search" size="small" severity="secondary" class="mt-3"
          :loading="busy === 'check_files'" :disabled="busy !== null"
          data-test="store-run-check"
          @click="run('check_files', { filename: effectiveFileName }, 'Проверка файлов поставлена в очередь')" />
      </div>

      <!-- Слияние -->
      <div class="rounded-lg border border-slate-200 p-4" data-test="store-merge-form">
        <h4 class="text-sm font-semibold">Слияние</h4>
        <p class="mt-2 text-xs magaz-muted">Слияние файла «{{ effectiveFileName }}» ({{ fileForm.is_extension ? 'расширение .cfe' : 'основная .cf' }}) в базу.</p>
        <Button label="Запустить слияние" size="small" class="mt-3" :loading="busy === 'run_merge'"
          :disabled="busy !== null" data-test="store-run-merge"
          @click="run('run_merge', mergeParams(), 'Слияние поставлено в очередь')" />
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
