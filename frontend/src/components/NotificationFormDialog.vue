<script setup lang="ts">
/**
 * Форма уведомления (§11.5, ФН3, Э5): «Создать уведомление».
 *
 * Здесь же задаётся адресация («все / кластер / список магазинов»), необходимость
 * «Ознакомлен», дедлайн, интервал повтора для `critical` и дублирование через
 * `msg.exe` (§4.6). Интервал возврата отложенного здесь не настраивается: это
 * настройка сервера `notification_snooze_min` (§4.9, П-2), она едет на кассу
 * вместе с уведомлением.
 */
import { computed, reactive, ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'

import type { NotificationCreateResponse, NotificationTargetType } from '@/api/types'
import { SEVERITY_LABELS, SEVERITY_OPTIONS, useNotificationsStore } from '@/stores/notifications'

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

const needsStores = computed(() => form.target_type === 'stores')
const needsCluster = computed(() => form.target_type === 'cluster')

/** Заполнить форму значениями по умолчанию при открытии. */
function reset(): void {
  form.title = ''
  form.body = ''
  form.severity = 'important'
  form.requires_ack = true
  form.target_type = 'all'
  form.store_ids = []
  form.cluster = props.clusterOptions[0] ?? ''
  form.deadline = ''
  form.repeat_every_min = ''
  form.use_msg_exe = true
  formError.value = ''
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
  if (!form.title.trim() || !form.body.trim()) {
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
    body: form.body.trim(),
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

      <label class="space-y-1 md:col-span-2">
        <span class="magaz-muted text-xs">Текст</span>
        <textarea
          v-model="form.body"
          rows="3"
          class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
          placeholder="Сохраните смену и отключите кассу"
          data-test="notify-form-body"
        ></textarea>
      </label>

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
</template>
