<script setup lang="ts">
/**
 * Оборудование подсети в карточке магазина (§11.2, §11.7, Д-1, Э4Б).
 *
 * Список и «Проверить сейчас» работают по магазину карточки (`GET/POST
 * /admin/stores/{id}/devices*`). Подсеть опрашивает касса, поэтому проверка —
 * это запрос сервера к агенту, а серый статус «нет данных» означает «касса не
 * ответила», а не «устройство пропало» (§4.11).
 *
 * Форма добавления и правки — общий компонент `DeviceFormDialog.vue`.
 */
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'

import { ApiError } from '@/api/client'
import { devicesApi } from '@/api/endpoints'
import type { DeviceCheckResponse, DeviceItem } from '@/api/types'
import ConfirmDeleteDialog from '@/components/ConfirmDeleteDialog.vue'
import DeviceFormDialog from '@/components/DeviceFormDialog.vue'
import {
  checkTypeLabel,
  deviceStatusLabel,
  deviceStatusSeverity,
  deviceTypeLabel,
} from '@/stores/devices'

const props = defineProps<{ storeId: number }>()

const toast = useToast()

const devices = ref<DeviceItem[]>([])
const isLoading = ref(false)
const isChecking = ref(false)
const errorMessage = ref('')
const lastCheck = ref<DeviceCheckResponse | null>(null)
const includeInactive = ref(true)

const isFormOpen = ref(false)
const editing = ref<DeviceItem | null>(null)

const activeCount = computed(() => devices.value.filter((item) => item.is_active).length)
const onlineCount = computed(
  () => devices.value.filter((item) => item.is_active && item.is_online).length,
)

async function load(): Promise<void> {
  isLoading.value = true
  try {
    devices.value = await devicesApi.list(props.storeId, includeInactive.value)
    errorMessage.value = ''
  } catch (error) {
    errorMessage.value =
      error instanceof ApiError ? error.message : 'Не удалось получить список оборудования'
  } finally {
    isLoading.value = false
  }
}

watch(
  () => props.storeId,
  () => {
    void load()
  },
  { immediate: true },
)

function openCreate(): void {
  editing.value = null
  copyFrom.value = null
  isFormOpen.value = true
}

function openEdit(device: DeviceItem): void {
  editing.value = device
  copyFrom.value = null
  isFormOpen.value = true
}

/** «Скопировать» (Д-23): создать устройство по образцу — все поля, кроме имени. */
const copyFrom = ref<DeviceItem | null>(null)
function openCopy(device: DeviceItem): void {
  copyFrom.value = device
  editing.value = null
  isFormOpen.value = true
}

async function onSaved(): Promise<void> {
  await load()
  toast.add({ severity: 'success', summary: 'Устройство сохранено', life: 4000 })
}

/** Э10: «Удалить» — строка исчезает насовсем, поэтому сначала подтверждение. */
const pendingDelete = ref<DeviceItem | null>(null)
const isDeleting = ref(false)

function askDelete(device: DeviceItem): void {
  pendingDelete.value = device
}

async function confirmDelete(): Promise<void> {
  const device = pendingDelete.value
  if (!device) return
  isDeleting.value = true
  try {
    await devicesApi.purge(props.storeId, device.id)
    pendingDelete.value = null
    await load()
    toast.add({
      severity: 'success',
      summary: 'Устройство удалено из списка',
      detail: `${device.name} (${device.ip_address})`,
      life: 5000,
    })
  } catch (error) {
    notifyError(error, 'Не удалось удалить устройство')
  } finally {
    isDeleting.value = false
  }
}

function notifyError(error: unknown, fallback: string): void {
  const message = error instanceof ApiError ? error.message : fallback
  toast.add({ severity: 'error', summary: 'Операция не выполнена', detail: message, life: 6000 })
}

/** «Проверить сейчас»: пусто — весь магазин, список id — только выбранные строки (§11.7). */
async function checkNow(device?: DeviceItem): Promise<void> {
  isChecking.value = true
  try {
    lastCheck.value = await devicesApi.check(props.storeId, device ? [device.id] : [])
    await load()
  } catch (error) {
    lastCheck.value = null
    notifyError(error, 'Не удалось выполнить проверку')
  } finally {
    isChecking.value = false
  }
}

async function deactivate(device: DeviceItem): Promise<void> {
  try {
    await devicesApi.deactivate(props.storeId, device.id)
    await load()
    toast.add({
      severity: 'success',
      summary: 'Устройство отключено из опроса',
      detail: device.name,
      life: 4000,
    })
  } catch (error) {
    notifyError(error, 'Не удалось отключить устройство')
  }
}

async function restore(device: DeviceItem): Promise<void> {
  try {
    await devicesApi.update(props.storeId, device.id, { is_active: true })
    await load()
    toast.add({ severity: 'success', summary: 'Устройство вернулось в опрос', life: 4000 })
  } catch (error) {
    notifyError(error, 'Не удалось вернуть устройство в опрос')
  }
}

function formatTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU')
}
</script>

<template>
  <section class="rounded-xl border border-slate-200 bg-white p-5" data-test="store-devices">
    <header class="flex flex-wrap items-center justify-between gap-2">
      <div>
        <h3 class="font-semibold">Оборудование подсети</h3>
        <p class="magaz-muted text-xs">
          Камеры, свитчи, принтеры и ПК магазина: опрашивает касса — по расписанию и кнопкой
          «Проверить сейчас» (Д-1).
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <span class="magaz-muted text-xs" data-test="store-devices-count">
          онлайн {{ onlineCount }} из {{ activeCount }}
        </span>
        <label class="magaz-muted flex items-center gap-2 text-xs">
          <input
            v-model="includeInactive"
            type="checkbox"
            data-test="store-devices-include-inactive"
            @change="load()"
          />
          показывать отключённые
        </label>
        <Button
          label="Проверить сейчас"
          icon="pi pi-bolt"
          size="small"
          :loading="isChecking"
          :disabled="!activeCount"
          data-test="store-devices-check"
          @click="checkNow()"
        />
        <Button
          label="Добавить устройство"
          icon="pi pi-plus"
          size="small"
          severity="secondary"
          data-test="store-devices-add"
          @click="openCreate"
        />
      </div>
    </header>

    <Message v-if="errorMessage" severity="error" :closable="false" class="mt-3">
      {{ errorMessage }}
    </Message>
    <Message
      v-if="lastCheck"
      :severity="lastCheck.agent_ok ? 'success' : 'warn'"
      :closable="false"
      class="mt-3"
      data-test="store-devices-check-result"
    >
      {{ lastCheck.message }}
    </Message>

    <div class="mt-3 overflow-x-auto">
      <table class="magaz-table">
        <thead>
          <tr>
            <th>Имя</th>
            <th class="w-40">IP</th>
            <th class="w-28">Тип</th>
            <th class="w-40">Проверка</th>
            <th class="w-28">Статус</th>
            <th class="w-24">Задержка</th>
            <th class="w-44">Последний ответ</th>
            <th class="w-64">Действия</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="device in devices" :key="device.id" :data-test="`store-device-${device.id}`">
            <td>
              {{ device.name }}
              <p v-if="!device.is_active" class="magaz-muted text-xs">отключено из опроса</p>
              <p v-else-if="device.comment" class="magaz-muted text-xs">{{ device.comment }}</p>
            </td>
            <td class="magaz-mono">{{ device.ip_address }}</td>
            <td>{{ deviceTypeLabel(device.device_type) }}</td>
            <td>{{ checkTypeLabel(device.check_type, device.check_port) }}</td>
            <td>
              <Tag
                :value="deviceStatusLabel(device.last_status)"
                :severity="deviceStatusSeverity(device.last_status)"
              />
            </td>
            <td>{{ device.last_latency_ms === null ? '—' : `${device.last_latency_ms} мс` }}</td>
            <td class="magaz-muted whitespace-nowrap">
              {{ formatTime(device.last_seen_at) }}
              <span v-if="device.last_error" class="block text-xs" :title="device.last_error">
                {{ device.last_error }}
              </span>
            </td>
            <td>
              <div class="flex flex-wrap gap-1">
                <Button
                  label="Проверить"
                  size="small"
                  text
                  :disabled="!device.is_active"
                  @click="checkNow(device)"
                />
                <Button label="Скопировать" size="small" text :data-test="`equipment-copy-${device.id}`" @click="openCopy(device)" />
                <Button label="Изменить" size="small" text @click="openEdit(device)" />
                <Button
                  v-if="device.is_active"
                  label="Отключить"
                  size="small"
                  text
                  severity="danger"
                  @click="deactivate(device)"
                />
                <Button v-else label="Вернуть" size="small" text @click="restore(device)" />
                <Button
                  label="Удалить"
                  size="small"
                  text
                  severity="danger"
                  :data-test="`equipment-delete-${device.id}`"
                  @click="askDelete(device)"
                />
              </div>
            </td>
          </tr>
          <tr v-if="!devices.length && !isLoading">
            <td colspan="8" class="magaz-muted py-6 text-center" data-test="store-devices-empty">
              Устройств нет: добавьте камеры, свитчи и прочую технику подсети — их будет опрашивать
              касса.
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <DeviceFormDialog
      v-model:visible="isFormOpen"
      :store-id="storeId"
      :device="editing"
      :copy-from="copyFrom"
      @saved="onSaved"
    />

    <ConfirmDeleteDialog
      :visible="pendingDelete !== null"
      header="Удалить устройство из списка?"
      :message="`Устройство «${pendingDelete?.name ?? ''}» (${pendingDelete?.ip_address ?? ''}) исчезнет из списка магазина, вернуть его будет нельзя.`"
      :details="[
        'История статусов и задержек по этому адресу будет потеряна',
        'Касса получит новый список устройств и перестанет его опрашивать',
        'Если нужна только пауза — используйте «Отключить»: строка и история останутся',
      ]"
      :loading="isDeleting"
      @update:visible="(value: boolean) => { if (!value) pendingDelete = null }"
      @confirm="confirmDelete"
    />
  </section>
</template>

