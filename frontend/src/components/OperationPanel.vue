<script setup lang="ts">
/**
 * Панель операций (§11.4, Э4) — одна форма запуска для магазина, кластера или всех.
 *
 * Компонент намеренно не знает, откуда пришёл выбор: ему передают `storeIds`,
 * `cluster` или `all`, а он вызывает `dispatchAction`, который сам выбирает между
 * `POST /stores/{id}/actions` и `POST /bulk/actions`. Благодаря этому панель
 * одинаково работает и в карточке магазина, и на странице файлов, и для «всех».
 *
 * Все операции отвечают `202` — это «принято в работу», а не «выполнено»:
 * результат придёт в историю задач (`/jobs`) и в логи (§11.6).
 */
import { computed, reactive, ref } from 'vue'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import { useToast } from 'primevue/usetoast'

import { ApiError } from '@/api/client'
import { dispatchAction, type ActionOutcome } from '@/api/actions'
import type { ActionName } from '@/api/types'

const props = withDefaults(
  defineProps<{
    storeIds?: number[]
    cluster?: string | null
    all?: boolean
    title?: string
    /** Показать блок «Проверить файлы» (§11.3) */
    showCheckFiles?: boolean
    /** Показать блок «Разослать ссылку» (ФН2, §11.3) */
    showDownloadLink?: boolean
    defaultFileName?: string
    /** Компактный режим — для карточки магазина */
    compact?: boolean
  }>(),
  {
    storeIds: () => [],
    cluster: null,
    all: false,
    title: 'Операции',
    showCheckFiles: true,
    showDownloadLink: false,
    defaultFileName: 'update',
    compact: false,
  },
)

const emit = defineEmits<{ submitted: [outcome: ActionOutcome] }>()

const toast = useToast()

const merge = reactive({
  file_name: props.defaultFileName,
  full_merge: false,
  extension_name: '',
})
const update = reactive({ is_fast_update: false, extension_name: '' })
const message = reactive({ text_message: '' })
const check = reactive({ filename: props.defaultFileName })
const link = reactive({ url: '' })

const busyAction = ref<ActionName | null>(null)
const warning = ref('')
const errorMessage = ref('')
/** Конфликт R12 (409): запуск отменён, потому что ИБ уже обновляется (§11.4) */
const conflict = ref('')
const lastOutcome = ref<ActionOutcome | null>(null)

/** Что показать в шапке панели: кто получит операцию. */
const targetLabel = computed(() => {
  if (props.all) return 'все активные магазины'
  if (props.cluster) return `кластер «${props.cluster}»`
  if (props.storeIds.length === 1) return 'этот магазин'
  if (props.storeIds.length > 1) return `выбрано магазинов: ${props.storeIds.length}`
  return 'все активные магазины'
})

const targets = computed(() => ({
  storeIds: props.storeIds,
  cluster: props.cluster,
  all: props.all || (!props.storeIds.length && !props.cluster),
}))

const isDisabled = computed(() => busyAction.value !== null)

function notifyError(error: unknown, fallback: string): void {
  const detail =
    error instanceof ApiError && error.messages.length > 0
      ? error.messages.join('; ')
      : error instanceof ApiError
        ? error.message
        : fallback

  // 409 — не «поломка», а запрет (§5.2.2, R12/FR-28): показываем его спокойным
  // жёлтым блоком и объясняем, что исключить из запуска, а не красной ошибкой.
  if (error instanceof ApiError && error.isConflict) {
    conflict.value = detail
    toast.add({ severity: 'warn', summary: 'Конфликт запуска (R12)', detail, life: 8000 })
    return
  }

  errorMessage.value = detail
  toast.add({ severity: 'error', summary: 'Операция не выполнена', detail, life: 6000 })
}

async function run(
  action: ActionName,
  params: Record<string, unknown>,
  successSummary: string,
): Promise<void> {
  if (isDisabled.value) return
  busyAction.value = action
  warning.value = ''
  errorMessage.value = ''
  conflict.value = ''

  try {
    const outcome = await dispatchAction(targets.value, action, params)
    lastOutcome.value = outcome
    warning.value = outcome.warning ?? ''
    toast.add({
      severity: outcome.warning ? 'warn' : 'success',
      summary: successSummary,
      detail: outcome.warning ?? outcome.message,
      life: 6000,
    })
    emit('submitted', outcome)
  } catch (error) {
    notifyError(error, 'Сервер не принял операцию')
  } finally {
    busyAction.value = null
  }
}
</script>

<template>
  <section class="rounded-xl border border-slate-200 bg-white p-5">
    <header class="flex flex-wrap items-center justify-between gap-2">
      <div>
        <h3 class="font-semibold">{{ title }}</h3>
        <p class="magaz-muted text-xs">
          Получатель: <span class="magaz-mono">{{ targetLabel }}</span> · сервер ставит задачу в
          очередь и отвечает <span class="magaz-mono">202</span>
        </p>
      </div>
      <span v-if="lastOutcome" class="magaz-muted text-xs" data-test="operation-jobs">
        задач: {{ lastOutcome.jobs.length
        }}<template v-if="lastOutcome.skipped"> · пропущено: {{ lastOutcome.skipped }}</template>
      </span>
    </header>

    <Message
      v-if="warning"
      severity="warn"
      :closable="false"
      class="mt-3"
      data-test="operation-warning"
    >
      {{ warning }}
    </Message>
    <Message
      v-if="conflict"
      severity="warn"
      :closable="false"
      class="mt-3"
      data-test="operation-conflict"
    >
      <span class="font-semibold">Запуск отменён: одна серверная ИБ не обновляется дважды (R12).</span>
      <span class="mt-1 block">{{ conflict }}</span>
    </Message>
    <Message v-if="errorMessage" severity="error" :closable="false" class="mt-3">
      {{ errorMessage }}
    </Message>

    <div class="mt-4 grid gap-4" :class="compact ? '' : 'lg:grid-cols-2'">
      <!-- Слияние конфигурации / расширения (FR-3, FR-4) -->
      <div class="rounded-lg border border-slate-200 p-4" data-test="merge-form">
        <h4 class="text-sm font-semibold">Слияние конфигурации</h4>
        <div class="mt-2 space-y-2">
          <label class="block space-y-1">
            <span class="magaz-muted text-xs">Имя файла (без расширения)</span>
            <InputText
              v-model="merge.file_name"
              class="w-full"
              :pt="{ root: { 'data-test': 'merge-file-name' } }"
            />
          </label>
          <label class="block space-y-1">
            <span class="magaz-muted text-xs">Расширение (опционально)</span>
            <InputText
              v-model="merge.extension_name"
              class="w-full"
              placeholder="имя расширения"
              :pt="{ root: { 'data-test': 'merge-extension' } }"
            />
          </label>
          <label class="flex items-center gap-2 text-sm">
            <Checkbox v-model="merge.full_merge" binary input-id="full-merge" />
            <span>Слияние с <span class="magaz-mono">full_merge.xml</span></span>
          </label>
          <Button
            label="Запустить слияние"
            icon="pi pi-play"
            size="small"
            :loading="busyAction === 'run_merge'"
            :disabled="isDisabled || !merge.file_name.trim()"
            data-test="run-merge"
            @click="
              run(
                'run_merge',
                {
                  file_name: merge.file_name.trim(),
                  full_merge: merge.full_merge,
                  ...(merge.extension_name.trim()
                    ? { extension_name: merge.extension_name.trim() }
                    : {}),
                },
                'Слияние поставлено в очередь',
              )
            "
          />
        </div>
      </div>

      <!-- Обновление конфигурации БД (FR-5, FR-6) -->
      <div class="rounded-lg border border-slate-200 p-4" data-test="update-form">
        <h4 class="text-sm font-semibold">Обновление конфигурации БД</h4>
        <div class="mt-2 space-y-2">
          <label class="block space-y-1">
            <span class="magaz-muted text-xs">Расширение (опционально)</span>
            <InputText
              v-model="update.extension_name"
              class="w-full"
              placeholder="имя расширения"
              :pt="{ root: { 'data-test': 'update-extension' } }"
            />
          </label>
          <label class="flex items-center gap-2 text-sm">
            <Checkbox v-model="update.is_fast_update" binary input-id="fast-update" />
            <span>Быстрое обновление (без завершения сеансов)</span>
          </label>
          <p class="magaz-muted text-xs">
            Полный цикл: завершение сеансов → <span class="magaz-mono">taskkill 1cv8.exe</span> →
            обновление → разрешение работы пользователей.
          </p>
          <Button
            label="Обновить базу"
            icon="pi pi-database"
            size="small"
            :loading="busyAction === 'run_update_db'"
            :disabled="isDisabled"
            data-test="run-update-db"
            @click="
              run(
                'run_update_db',
                {
                  is_fast_update: update.is_fast_update,
                  ...(update.extension_name.trim()
                    ? { extension_name: update.extension_name.trim() }
                    : {}),
                },
                'Обновление БД поставлено в очередь',
              )
            "
          />
        </div>
      </div>

      <!-- Проверка файлов (§11.3) -->
      <div
        v-if="showCheckFiles"
        class="rounded-lg border border-slate-200 p-4"
        data-test="check-form"
      >
        <h4 class="text-sm font-semibold">Проверить файлы обновления</h4>
        <div class="mt-2 space-y-2">
          <label class="block space-y-1">
            <span class="magaz-muted text-xs">Имя файла (без расширения)</span>
            <InputText
              v-model="check.filename"
              class="w-full"
              :pt="{ root: { 'data-test': 'check-filename' } }"
            />
          </label>
          <Button
            label="Проверить"
            icon="pi pi-search"
            size="small"
            severity="secondary"
            :loading="busyAction === 'check_files'"
            :disabled="isDisabled || !check.filename.trim()"
            data-test="run-check-files"
            @click="
              run(
                'check_files',
                { filename: check.filename.trim() },
                'Проверка поставлена в очередь',
              )
            "
          />
        </div>
      </div>

      <!-- Сообщение пользователям (FR-8) -->
      <div class="rounded-lg border border-slate-200 p-4" data-test="message-form">
        <h4 class="text-sm font-semibold">Сообщение пользователям</h4>
        <div class="mt-2 space-y-2">
          <label class="block space-y-1">
            <span class="magaz-muted text-xs">Текст окна</span>
            <InputText
              v-model="message.text_message"
              class="w-full"
              placeholder="База будет обновлена через 5 минут"
              :pt="{ root: { 'data-test': 'message-text' } }"
            />
          </label>
          <p class="text-xs text-amber-700">
            Внимание: команда завершит <span class="magaz-mono">1cv8.exe</span> у всех
            пользователей — предупредите их этим сообщением.
          </p>
          <Button
            label="Отправить сообщение"
            icon="pi pi-send"
            size="small"
            severity="warn"
            :loading="busyAction === 'send_message'"
            :disabled="isDisabled || !message.text_message.trim()"
            data-test="run-send-message"
            @click="
              run(
                'send_message',
                { text_message: message.text_message.trim() },
                'Сообщение отправлено',
              )
            "
          />
        </div>
      </div>

      <!-- Разослать ссылку (ФН2, §11.3) -->
      <div
        v-if="showDownloadLink"
        class="rounded-lg border border-slate-200 p-4"
        data-test="link-form"
      >
        <h4 class="text-sm font-semibold">Разослать ссылку на пакет (ФН2)</h4>
        <div class="mt-2 space-y-2">
          <label class="block space-y-1">
            <span class="magaz-muted text-xs">Ссылка Яндекс.Диска</span>
            <InputText
              v-model="link.url"
              class="w-full"
              placeholder="https://disk.yandex.ru/d/XXXX"
              :pt="{ root: { 'data-test': 'link-url' } }"
            />
          </label>
          <p class="magaz-muted text-xs">
            После скачивания сервер сам закажет повторную проверку файлов (§11.3).
          </p>
          <Button
            label="Скачать на магазинах"
            icon="pi pi-download"
            size="small"
            :loading="busyAction === 'download_file'"
            :disabled="isDisabled || !link.url.trim()"
            data-test="run-download-file"
            @click="
              run(
                'download_file',
                { 'url-file': link.url.trim() },
                'Скачивание поставлено в очередь',
              )
            "
          />
        </div>
      </div>
    </div>
  </section>
</template>
