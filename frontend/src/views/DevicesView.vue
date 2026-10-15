<script setup lang="ts">
/**
 * Оборудование подсети (§11.7, Д-1, Э4Б).
 *
 * Сводная таблица «магазин × устройство»: что за техника заведена в подсети,
 * чем её проверяют (`ping` или TCP-порт) и когда она отвечала. Подсеть
 * опрашивает касса, поэтому «Проверить сейчас» — это запрос сервера к агенту,
 * а серый статус «нет данных» означает «касса не ответила», а не «устройство
 * пропало» (§4.11).
 *
 * Плитка главной ведёт сюда с фильтром «офлайн» (`?status=offline`) — админ
 * сразу видит, что именно отвалилось (§11.1).
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'

import type { DeviceCheckResponse, DeviceItem, DeviceStatusFilter } from '@/api/types'
import ConfirmDeleteDialog from '@/components/ConfirmDeleteDialog.vue'
import DeviceFormDialog from '@/components/DeviceFormDialog.vue'
import {
  DEVICE_STATUS_OPTIONS,
  DEVICE_TYPE_LABELS,
  DEVICE_TYPE_OPTIONS,
  checkTypeLabel,
  deviceStatusLabel,
  deviceStatusSeverity,
  deviceTypeLabel,
  useDevicesStore,
} from '@/stores/devices'
import { useStoresStore } from '@/stores/stores'

const route = useRoute()
const toast = useToast()
const devices = useDevicesStore()
const stores = useStoresStore()

const isFormOpen = ref(false)
const editing = ref<DeviceItem | null>(null)
const isChecking = ref(false)
const lastCheck = ref<DeviceCheckResponse | null>(null)

const storeOptions = computed(() => stores.items.map((item) => ({ id: item.id, name: item.name })))

const description = computed(() => {
  if (!devices.total) return 'Устройств пока нет'
  return `Онлайн ${devices.online} из ${devices.total} (окно ${devices.windowSec} с) · офлайн ${devices.offline} · без данных ${devices.unknown}`
})

onMounted(async () => {
  // Плитка главной передаёт фильтр в адресе: `?status=offline` (§11.1, §11.7)
  const status = route.query.status
  if (typeof status === 'string' && ['online', 'offline', 'unknown'].includes(status)) {
    devices.status = status as DeviceStatusFilter
  }
  const storeId = route.query.store_id
  if (typeof storeId === 'string' && Number(storeId) > 0) {
    devices.storeId = Number(storeId)
  }

  if (!stores.items.length) await stores.load()
  devices.startPolling()
})

onUnmounted(() => devices.stopPolling())

function notifyError(error: unknown, fallback: string): void {
  const message = error instanceof Error ? error.message : fallback
  toast.add({ severity: 'error', summary: 'Операция не выполнена', detail: message, life: 6000 })
}

/** «Проверить сейчас» по строке (§11.7): сервер просит кассу проверить тот же адрес. */
async function checkRow(device: DeviceItem): Promise<void> {
  isChecking.value = true
  try {
    lastCheck.value = await devices.check(device.store_id, [device.id])
    if (lastCheck.value) {
      toast.add({
        severity: lastCheck.value.agent_ok ? 'success' : 'warn',
        summary: 'Проверка выполнена',
        detail: lastCheck.value.message,
        life: 6000,
      })
    }
  } finally {
    isChecking.value = false
  }
}

/** «Проверить сейчас» по магазину: адреса берутся из фильтра «Магазин» (§11.7). */
async function checkStore(): Promise<void> {
  if (!devices.storeId) {
    toast.add({
      severity: 'warn',
      summary: 'Выберите магазин',
      detail: 'Проверка по магазину опрашивает все его устройства — нужен фильтр «Магазин»',
      life: 6000,
    })
    return
  }
  isChecking.value = true
  try {
    lastCheck.value = await devices.check(devices.storeId)
    if (lastCheck.value) {
      toast.add({
        severity: lastCheck.value.agent_ok ? 'success' : 'warn',
        summary: 'Проверка магазина выполнена',
        detail: lastCheck.value.message,
        life: 6000,
      })
    }
  } finally {
    isChecking.value = false
  }
}

function openCreate(): void {
  editing.value = null
  isFormOpen.value = true
}

function openEdit(device: DeviceItem): void {
  editing.value = device
  isFormOpen.value = true
}

async function onSaved(): Promise<void> {
  await devices.load()
  toast.add({ severity: 'success', summary: 'Устройство сохранено', life: 4000 })
}

async function deactivate(device: DeviceItem): Promise<void> {
  try {
    await devices.deactivate(device.store_id, device.id)
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
    await devices.restore(device.store_id, device.id)
    toast.add({ severity: 'success', summary: 'Устройство вернулось в опрос', life: 4000 })
  } catch (error) {
    notifyError(error, 'Не удалось вернуть устройство в опрос')
  }
}

/** Э10: «Удалить» — подтверждение, потому что строку вернуть уже нельзя. */
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
    await devices.purge(device.store_id, device.id)
    pendingDelete.value = null
    toast.add({
      severity: 'success',
      summary: 'Устройство удалено из списка',
      detail: `${device.name} (${device.ip_address}) — касса получит новый список при следующем опросе`,
      life: 6000,
    })
  } catch (error) {
    notifyError(error, 'Не удалось удалить устройство')
  } finally {
    isDeleting.value = false
  }
}

function formatTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU')
}
</script>

<template>
  <div class="space-y-6">
    <header class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h2 class="text-xl font-semibold">Оборудование подсети</h2>
        <p class="magaz-muted text-sm">
          Камеры, свитчи, принтеры и ПК магазинов: чем проверяются, что отвечало и что молчит.
        </p>
        <p class="magaz-muted text-xs" data-test="devices-summary">{{ description }}</p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <span v-if="devices.lastLoadedAt" class="magaz-muted text-xs">
          обновлено {{ devices.lastLoadedAt.toLocaleTimeString('ru-RU') }}
        </span>
        <Button
          label="Проверить магазин"
          icon="pi pi-bolt"
          size="small"
          :loading="isChecking"
          data-test="devices-check-store"
          @click="checkStore"
        />
        <Button
          label="Добавить устройство"
          icon="pi pi-plus"
          size="small"
          severity="secondary"
          data-test="devices-add"
          @click="openCreate"
        />
      </div>
    </header>

    <Message v-if="devices.errorMessage" severity="error" :closable="false" data-test="devices-error">
      {{ devices.errorMessage }}
    </Message>
    <Message
      v-if="lastCheck"
      :severity="lastCheck.agent_ok ? 'success' : 'warn'"
      :closable="false"
      data-test="devices-check-result"
    >
      {{ lastCheck.message }}
    </Message>

    <div class="flex flex-wrap items-center gap-3">
      <InputText
        v-model="devices.search"
        placeholder="Поиск: имя, IP"
        class="w-64"
        data-test="devices-search"
        @keyup.enter="devices.load()"
      />
      <select
        v-model="devices.storeId"
        class="rounded-lg border border-slate-300 px-3 py-2 text-sm"
        data-test="devices-store-filter"
        @change="devices.load()"
      >
        <option :value="null">Все магазины</option>
        <option v-for="item in storeOptions" :key="item.id" :value="item.id">{{ item.name }}</option>
      </select>
      <select
        v-model="devices.cluster"
        class="rounded-lg border border-slate-300 px-3 py-2 text-sm"
        data-test="devices-cluster-filter"
        @change="devices.load()"
      >
        <option value="">Все кластеры</option>
        <option v-for="item in devices.clusters" :key="item" :value="item">{{ item }}</option>
      </select>
      <select
        v-model="devices.deviceType"
        class="rounded-lg border border-slate-300 px-3 py-2 text-sm"
        data-test="devices-type-filter"
        @change="devices.load()"
      >
        <option value="">Все типы</option>
        <option v-for="item in DEVICE_TYPE_OPTIONS" :key="item" :value="item">
          {{ DEVICE_TYPE_LABELS[item] }}
        </option>
      </select>
      <select
        v-model="devices.status"
        class="rounded-lg border border-slate-300 px-3 py-2 text-sm"
        data-test="devices-status-filter"
        @change="devices.load()"
      >
        <option value="all">Все статусы</option>
        <option v-for="item in DEVICE_STATUS_OPTIONS" :key="item" :value="item">
          {{ deviceStatusLabel(item) }}
        </option>
      </select>
      <label class="magaz-muted flex items-center gap-2 text-sm">
        <input
          v-model="devices.includeInactive"
          type="checkbox"
          data-test="devices-include-inactive"
          @change="devices.load()"
        />
        показывать отключённые
      </label>
      <Button
        class="ml-auto"
        label="Обновить"
        icon="pi pi-refresh"
        size="small"
        text
        :loading="devices.isLoading"
        data-test="devices-refresh"
        @click="devices.load()"
      />
    </div>

    <section class="overflow-hidden rounded-xl border border-slate-200 bg-white">
      <table class="magaz-table">
        <thead>
          <tr>
            <th>Магазин</th>
            <th>Имя</th>
            <th class="w-40">IP</th>
            <th class="w-28">Тип</th>
            <th class="w-40">Проверка</th>
            <th class="w-28">Статус</th>
            <th class="w-24">Задержка</th>
            <th class="w-44">Последний ответ</th>
            <th class="w-56">Действия</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in devices.rows" :key="row.id" :data-test="`device-row-${row.id}`">
            <td>
              <RouterLink
                :to="{ name: 'store-detail', params: { id: row.store_id } }"
                class="font-medium text-sky-700 hover:underline"
              >
                {{ row.store_name ?? `id=${row.store_id}` }}
              </RouterLink>
              <p class="magaz-muted text-xs">
                {{ row.cluster_name ?? '—' }}
                <span v-if="!row.store_is_active"> · отключён</span>
                <span v-else-if="!row.store_is_online"> · касса офлайн</span>
              </p>
            </td>
            <td>
              {{ row.name }}
              <p v-if="!row.is_active" class="magaz-muted text-xs">отключено из опроса</p>
              <p v-else-if="row.comment" class="magaz-muted text-xs">{{ row.comment }}</p>
            </td>
            <td class="magaz-mono">{{ row.ip_address }}</td>
            <td>{{ deviceTypeLabel(row.device_type) }}</td>
            <td>{{ checkTypeLabel(row.check_type, row.check_port) }}</td>
            <td data-test="device-status">
              <Tag
                :value="deviceStatusLabel(row.last_status)"
                :severity="deviceStatusSeverity(row.last_status)"
              />
            </td>
            <td>{{ row.last_latency_ms === null ? '—' : `${row.last_latency_ms} мс` }}</td>
            <td class="magaz-muted whitespace-nowrap">
              {{ formatTime(row.last_seen_at) }}
              <span v-if="row.last_error" class="block text-xs" :title="row.last_error">
                {{ row.last_error }}
              </span>
            </td>
            <td>
              <div class="flex flex-wrap gap-1">
                <Button
                  label="Проверить"
                  size="small"
                  text
                  :disabled="!row.is_active || !row.store_is_active"
                  @click="checkRow(row)"
                />
                <Button label="Изменить" size="small" text @click="openEdit(row)" />
                <Button
                  v-if="row.is_active"
                  label="Отключить"
                  size="small"
                  text
                  severity="danger"
                  @click="deactivate(row)"
                />
                <Button v-else label="Вернуть" size="small" text @click="restore(row)" />
                <Button
                  label="Удалить"
                  size="small"
                  text
                  severity="danger"
                  :data-test="`device-delete-${row.id}`"
                  @click="askDelete(row)"
                />
              </div>
            </td>
          </tr>
          <tr v-if="!devices.rows.length && !devices.isLoading">
            <td colspan="9" class="magaz-muted py-6 text-center" data-test="devices-empty">
              Устройств нет. Откройте карточку магазина и добавьте технику его подсети (Д-1).
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <DeviceFormDialog
      v-model:visible="isFormOpen"
      :store-id="null"
      :store-options="storeOptions"
      :device="editing"
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
  </div>
</template>

