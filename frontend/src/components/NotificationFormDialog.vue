<script setup lang="ts">
/**
 * Форма уведомления (§11.5, ФН3, Э5): «Создать уведомление».
 *
 * Здесь же задаётся адресация («все / кластер / список магазинов»), необходимость
 * «Ознакомлен», дедлайн, интервал повтора для `critical` и дублирование через
 * `msg.exe` (§4.6). Интервал возврата отложенного здесь не настраивается: это
 * настройка сервера `notification_snooze_min` (§4.9, П-2), она едет на кассу
 * вместе с уведомлением.
 *
 * Э9: текст набирается в редакторе (`RichTextEditor`) — с разметкой и картинками,
 * а «Просмотреть» показывает то же сообщение, что увидит кассир
 * (`NotificationBody`). Картинки уезжают одним запросом вместе с созданием: файл
 * должен лежать на сервере до первой доставки (§7.4), поэтому черновики редактора
 * уходят в `images`, а в теле остаются метки `magaz-image:<имя>`.
 */
import { computed, reactive, ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'

import type {
  NotificationBodyFormat,
  NotificationCreateResponse,
  NotificationTargetType,
} from '@/api/types'
import NotificationBody from '@/components/NotificationBody.vue'
import RichTextEditor from '@/components/RichTextEditor.vue'
import {
  SEVERITY_LABELS,
  SEVERITY_OPTIONS,
  htmlToText,
  imageUpload,
  useNotificationsStore,
  type NotificationImageDraft,
} from '@/stores/notifications'

const props = withDefaults(
  defineProps<{
    visible: boolean
    storeOptions?: { id: number; name: string }[]
    clusterOptions?: string[]
  }>(),
  { storeOptions: () => [], clusterOptions: () => [] },
)

const emit = defineEmits<{
  'update:visible': [value: boolean]
  saved: [response: NotificationCreateResponse]
}>()

const notifications = useNotificationsStore()

const form = reactive({
  title: '',
  body: '',
  body_format: 'html' as NotificationBodyFormat,
  images: [] as NotificationImageDraft[],
  delete_after_read: false,
  severity: 'important',
  requires_ack: true,
  target_type: 'all' as NotificationTargetType,
  store_ids: [] as number[],
  cluster: '',
  deadline: '',
  repeat_every_min: '',
  use_msg_exe: true,
})
const formError = ref('')
const isPreviewOpen = ref(false)

const needsStores = computed(() => form.target_type === 'stores')
const needsCluster = computed(() => form.target_type === 'cluster')

/** Тело без разметки: у HTML-тела в пустом `<p></p>` текста нет (§4.6). */
const bodyPlainText = computed(() =>
  form.body_format === 'html' ? htmlToText(form.body) : form.body.trim(),
)

/** Заполнить форму значениями по умолчанию при открытии. */
function reset(): void {
  form.title = ''
  form.body = ''
  form.body_format = 'html'
  form.images = []
  form.delete_after_read = false
  form.severity = 'important'
  form.requires_ack = true
  form.target_type = 'all'
  form.store_ids = []
  form.cluster = props.clusterOptions[0] ?? ''
  form.deadline = ''
  form.repeat_every_min = ''
  form.use_msg_exe = true
  formError.value = ''
  isPreviewOpen.value = false
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) reset()
  },
)

function close(): void {
  emit('update:visible', false)
}

/** Дедлайн из `datetime-local` — в UTC ISO-8601 с суффиксом `Z` (§6). */
function deadlineIso(): string | null {
  if (!form.deadline.trim()) return null
  const date = new Date(form.deadline)
  if (Number.isNaN(date.getTime())) return null
  return date.toISOString()
}

async function submit(): Promise<void> {
  if (!form.title.trim() || !bodyPlainText.value) {
    formError.value = 'Заполните заголовок и текст уведомления'
    return
  }
  if (needsStores.value && !form.store_ids.length) {
    formError.value = 'Выберите хотя бы один магазин'
    return
  }
  if (needsCluster.value && !form.cluster.trim()) {
    formError.value = 'Укажите кластер'
    return
  }

  const repeat = form.repeat_every_min.trim() ? Number(form.repeat_every_min) : null
  const response = await notifications.create({
    title: form.title.trim(),
    body: form.body_format === 'html' ? form.body : form.body.trim(),
    body_format: form.body_format,
    // Картинки уезжают вместе с уведомлением: файл нужен кассе до первого показа (§7.4)
    images: form.images.map(imageUpload),
    delete_after_read: form.delete_after_read,
    severity: form.severity as 'info' | 'important' | 'critical',
    requires_ack: form.requires_ack,
    target_type: form.target_type,
    store_ids: needsStores.value ? form.store_ids : [],
    cluster: needsCluster.value ? form.cluster.trim() : null,
    deadline_at: deadlineIso(),
    repeat_every_min: repeat,
    use_msg_exe: form.use_msg_exe,
  })

  if (!response) {
    formError.value = notifications.errorMessage || 'Уведомление не создано'
    return
  }
  emit('saved', response)
  close()
}

/** «Просмотреть»: то же сообщение, что увидит кассир — разметка и картинки (§11.5). */
function openPreview(): void {
  if (!bodyPlainText.value) {
    formError.value = 'Показывать нечего: тело пустое'
    return
  }
  formError.value = ''
  isPreviewOpen.value = true
}

/** Редактор сам убирает картинку из тела — форме остаётся показать это в черновиках. */
function onDraftsUpdate(value: NotificationImageDraft[]): void {
  form.images = value
}
</script>

<template>
  <Dialog
    :visible="visible"
    modal
    header="Новое уведомление"
    :style="{ width: '720px' }"
    @update:visible="emit('update:visible', $event)"
  >
    <Message v-if="formError" severity="error" :closable="false" data-test="notify-form-error">
      {{ formError }}
    </Message>

    <div class="mt-3 grid gap-3 md:grid-cols-2">
      <label class="space-y-1 md:col-span-2">
        <span class="magaz-muted text-xs">Заголовок</span>
        <InputText
          v-model="form.title"
          class="w-full"
          placeholder="Смена закрывается в 22:00"
          :pt="{ root: { 'data-test': 'notify-form-title' } }"
        />
      </label>

      <div class="space-y-1 md:col-span-2">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <span class="magaz-muted text-xs">Текст</span>
          <div class="flex items-center gap-3">
            <label class="magaz-muted flex items-center gap-2 text-xs">
              <input
                type="checkbox"
                :checked="form.body_format === 'plain'"
                data-test="notify-form-plain"
                @change="
                  form.body_format = ($event.target as HTMLInputElement).checked ? 'plain' : 'html'
                "
              />
              простой текст (без разметки)
            </label>
            <Button
              label="Просмотреть"
              size="small"
              text
              data-test="notify-form-preview"
              @click="openPreview"
            />
          </div>
        </div>

        <textarea
          v-if="form.body_format === 'plain'"
          v-model="form.body"
          rows="4"
          class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
          placeholder="Сохраните смену и отключите кассу"
          data-test="notify-form-body"
        ></textarea>
        <RichTextEditor
          v-else
          v-model="form.body"
          :drafts="form.images"
          data-test="notify-form-editor"
          @update:drafts="onDraftsUpdate"
        />
      </div>

      <label class="space-y-1">
        <span class="magaz-muted text-xs">Уровень важности</span>
        <select
          v-model="form.severity"
          class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
          data-test="notify-form-severity"
        >
          <option v-for="item in SEVERITY_OPTIONS" :key="item" :value="item">
            {{ SEVERITY_LABELS[item] }}
          </option>
        </select>
      </label>

      <label class="space-y-1">
        <span class="magaz-muted text-xs">Адресация</span>
        <select
          v-model="form.target_type"
          class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
          data-test="notify-form-target"
        >
          <option value="all">все магазины</option>
          <option value="cluster">кластер</option>
          <option value="stores">выбранные магазины</option>
        </select>
      </label>

      <label v-if="needsCluster" class="space-y-1">
        <span class="magaz-muted text-xs">Кластер (серверная ИБ)</span>
        <select
          v-model="form.cluster"
          class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
          data-test="notify-form-cluster"
        >
          <option v-for="item in clusterOptions" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>

      <label v-if="needsStores" class="space-y-1 md:col-span-2">
        <span class="magaz-muted text-xs">Магазины (Ctrl — выбрать несколько)</span>
        <select
          v-model="form.store_ids"
          multiple
          size="5"
          class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
          data-test="notify-form-stores"
        >
          <option v-for="item in storeOptions" :key="item.id" :value="item.id">
            {{ item.name }}
          </option>
        </select>
      </label>

      <label class="space-y-1">
        <span class="magaz-muted text-xs">Дедлайн ознакомления (необязательно)</span>
        <input
          v-model="form.deadline"
          type="datetime-local"
          class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
          data-test="notify-form-deadline"
        />
      </label>

      <label class="space-y-1">
        <span class="magaz-muted text-xs">Повтор показа, мин (для critical)</span>
        <InputText
          v-model="form.repeat_every_min"
          class="w-full"
          placeholder="например: 30"
          :pt="{ root: { 'data-test': 'notify-form-repeat' } }"
        />
      </label>

      <label class="magaz-muted flex items-center gap-2 text-sm">
        <input v-model="form.requires_ack" type="checkbox" data-test="notify-form-ack" />
        требовать «Ознакомлен»
      </label>
      <label class="magaz-muted flex items-center gap-2 text-sm">
        <input v-model="form.use_msg_exe" type="checkbox" data-test="notify-form-msg" />
        дублировать важные через `msg.exe`
      </label>
      <label class="magaz-muted flex items-center gap-2 text-sm">
        <input
          v-model="form.delete_after_read"
          type="checkbox"
          data-test="notify-form-delete-after-read"
        />
        удалять после прочтения (иначе остаётся справочником на кассе)
      </label>
    </div>

    <p class="magaz-muted mt-3 text-xs">
      Доставку выполняет диспетчер: выключенная касса получит уведомление, когда вернётся в сеть
      (§11.4). «Отложить» возвращает окно через `notification_snooze_min` минут (§4.9, П-2).
    </p>

    <template #footer>
      <Button label="Отмена" text size="small" @click="close" />
      <Button
        label="Разослать"
        size="small"
        :loading="notifications.isSaving"
        data-test="notify-form-save"
        @click="submit"
      />
    </template>
  </Dialog>

  <Dialog
    :visible="isPreviewOpen"
    modal
    header="Так сообщение увидит кассир"
    :style="{ width: '640px' }"
    @update:visible="isPreviewOpen = $event"
  >
    <div class="rounded-lg border border-slate-200 bg-white px-4 py-3" data-test="notify-preview">
      <NotificationBody
        :body="form.body"
        :body-format="form.body_format"
        :drafts="form.images"
      />
    </div>
    <p class="magaz-muted mt-3 text-xs">
      Разметку и картинки проверяет сервер: `script` и внешние адреса до кассы не доедут (§4.6).
      Картинки уедут вместе с уведомлением и останутся в локальном кэше кассы (§7.4) — даже когда
      касса останется без сети.
    </p>
    <template #footer>
      <Button label="Закрыть" size="small" data-test="notify-preview-close" @click="isPreviewOpen = false" />
    </template>
  </Dialog>
</template>
