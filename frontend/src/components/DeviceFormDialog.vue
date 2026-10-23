<script setup lang="ts">
/**
 * Форма устройства подсети (§11.7, Д-1, Э4Б): «Добавить устройство» и «Изменить».
 *
 * Одна форма на страницу `/devices` и на карточку магазина: в карточке магазин
 * известен заранее (`storeId`), на странице `/devices` его выбирают в списке
 * (`storeOptions`), потому что таблица сводная.
 *
 * TCP-порт нужен только при `check_type=tcp`; при возврате к `icmp` форма
 * передаёт `clear_check_port`, иначе сервер отклонит пару «icmp + порт».
 */
import { computed, reactive, ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'

import { ApiError } from '@/api/client'
import { devicesApi } from '@/api/endpoints'
import type { DeviceCheckType, DeviceItem } from '@/api/types'
import { DEVICE_TYPE_LABELS, DEVICE_TYPE_OPTIONS } from '@/stores/devices'

const props = withDefaults(
  defineProps<{
    visible: boolean
    /** Магазин карточки; `null` — выбирается в форме (страница `/devices`) */
    storeId: number | null
    storeOptions?: { id: number; name: string }[]
    device?: DeviceItem | null
    /** Источник для «Скопировать»: поля заполняются, но имя пустое и это создание */
    copyFrom?: DeviceItem | null
  }>(),
  { storeOptions: () => [], device: null, copyFrom: null },
)

const emit = defineEmits<{
  'update:visible': [value: boolean]
  saved: [device: DeviceItem]
}>()

const form = reactive({
  storeId: null as number | null,
  name: '',
  ip_address: '',
  device_type: 'camera',
  check_type: 'icmp' as DeviceCheckType,
  check_port: '',
  comment: '',
})
const isSaving = ref(false)
const formError = ref('')

const isEdit = computed(() => props.device !== null)
const needsStore = computed(() => props.storeId === null)
const isPortNeeded = computed(() => form.check_type === 'tcp')

/** Заполнить форму из устройства (правка) или значениями по умолчанию (добавление). */
function reset(): void {
  const source = props.copyFrom ?? props.device
  form.storeId = props.storeId ?? props.storeOptions[0]?.id ?? null
  form.name = props.copyFrom ? '' : (source?.name ?? '')
  form.ip_address = source?.ip_address ?? ''
  form.device_type = source?.device_type ?? 'camera'
  form.check_type = source?.check_type ?? 'icmp'
  form.check_port = source?.check_port ? String(source.check_port) : ''
  form.comment = source?.comment ?? ''
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

async function submit(): Promise<void> {
  const targetStore = props.storeId ?? form.storeId
  if (!targetStore) {
    formError.value = 'Выберите магазин'
    return
  }
  if (!form.name.trim() || !form.ip_address.trim()) {
    formError.value = 'Заполните имя и IP-адрес устройства'
    return
  }

  const port = form.check_port.trim() ? Number(form.check_port) : null
  if (
    isPortNeeded.value &&
    (port === null || !Number.isInteger(port) || port < 1 || port > 65535)
  ) {
    formError.value = 'Для TCP-проверки нужен порт от 1 до 65535'
    return
  }

  isSaving.value = true
  formError.value = ''
  try {
    const payload = {
      name: form.name.trim(),
      ip_address: form.ip_address.trim(),
      device_type: form.device_type || null,
      check_type: form.check_type,
      check_port: isPortNeeded.value ? port : null,
      comment: form.comment.trim() || null,
    }

    const saved = props.device
      ? await devicesApi.update(targetStore, props.device.id, {
          ...payload,
          clear_check_port: !isPortNeeded.value,
        })
      : await devicesApi.create(targetStore, payload)

    emit('saved', saved)
    close()
  } catch (error) {
    formError.value =
      error instanceof ApiError && error.messages.length > 0
        ? error.messages.join('; ')
        : 'Не удалось сохранить устройство'
  } finally {
    isSaving.value = false
  }
}
</script>

<template>
  <Dialog
    :visible="visible"
    :header="isEdit ? 'Устройство подсети' : 'Новое устройство подсети'"
    modal
    :style="{ width: '640px' }"
    @update:visible="emit('update:visible', $event)"
  >
    <Message v-if="formError" severity="error" :closable="false" data-test="device-form-error">
      {{ formError }}
    </Message>

    <div class="mt-3 grid gap-3 md:grid-cols-2">
      <label v-if="needsStore" class="space-y-1 md:col-span-2">
        <span class="magaz-muted text-xs">Магазин</span>
        <select
          v-model="form.storeId"
          class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
          data-test="device-form-store"
        >
          <option :value="null" disabled>выберите магазин</option>
          <option v-for="item in storeOptions" :key="item.id" :value="item.id">
            {{ item.name }}
          </option>
        </select>
      </label>

      <label class="space-y-1">
        <span class="magaz-muted text-xs">Имя</span>
        <InputText
          v-model="form.name"
          class="w-full"
          placeholder="камера вход"
          :pt="{ root: { 'data-test': 'device-form-name' } }"
        />
      </label>
      <label class="space-y-1">
        <span class="magaz-muted text-xs">IP-адрес в подсети магазина</span>
        <InputText
          v-model="form.ip_address"
          class="w-full"
          placeholder="10.10.5.21"
          :pt="{ root: { 'data-test': 'device-form-ip' } }"
        />
      </label>

      <label class="space-y-1">
        <span class="magaz-muted text-xs">Тип устройства</span>
        <select
          v-model="form.device_type"
          class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
          data-test="device-form-type"
        >
          <option v-for="item in DEVICE_TYPE_OPTIONS" :key="item" :value="item">
            {{ DEVICE_TYPE_LABELS[item] }}
          </option>
        </select>
      </label>
      <label class="space-y-1">
        <span class="magaz-muted text-xs">Способ проверки</span>
        <select
          v-model="form.check_type"
          class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
          data-test="device-form-check-type"
        >
          <option value="icmp">ping (ICMP)</option>
          <option value="tcp">TCP-порт</option>
        </select>
      </label>

      <label v-if="isPortNeeded" class="space-y-1">
        <span class="magaz-muted text-xs">TCP-порт</span>
        <InputText
          v-model="form.check_port"
          class="w-full"
          placeholder="80"
          :pt="{ root: { 'data-test': 'device-form-port' } }"
        />
      </label>
      <label class="space-y-1 md:col-span-2">
        <span class="magaz-muted text-xs">Комментарий</span>
        <InputText
          v-model="form.comment"
          class="w-full"
          placeholder="например: над кассой №1"
          :pt="{ root: { 'data-test': 'device-form-comment' } }"
        />
      </label>
    </div>

    <p class="magaz-muted mt-3 text-xs">
      Опрашивает подсеть касса (она внутри сети магазина), поэтому адрес должен быть доступен с
      кассы, а не с сервера дашборда (§7.6).
    </p>

    <template #footer>
      <Button label="Отмена" text size="small" @click="close" />
      <Button
        label="Сохранить"
        size="small"
        :loading="isSaving"
        data-test="device-form-save"
        @click="submit"
      />
    </template>
  </Dialog>
</template>

