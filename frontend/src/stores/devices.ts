/**
 * Стор оборудования подсети (§11.7, Д-1, Э4Б).
 *
 * Страница `/devices` и плитка главной работают с одним сторем: сводка
 * «магазин × устройство» приходит из `/admin/devices/matrix`, а «онлайн»
 * считает сервер по окну `device_online_window_sec` (§4.11), поэтому кэш не
 * хранит собственных выводов о статусе — только то, что вернул API.
 *
 * Список оборудования в карточке магазина держит компонент
 * `StoreDevicesPanel.vue` (свой магазин — свой запрос), но пишет через те же
 * точки API, что и этот стор: `devicesApi`.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError } from '@/api/client'
import { devicesApi } from '@/api/endpoints'
import type {
  DeviceCheckResponse,
  DeviceCreatePayload,
  DeviceItem,
  DeviceListFilters,
  DeviceStatusFilter,
  DeviceUpdatePayload,
  DevicesMatrix,
} from '@/api/types'

/** Подписи типов оборудования (§4.11) для таблиц и фильтров. */
export const DEVICE_TYPE_LABELS: Record<string, string> = {
  camera: 'Камера',
  switch: 'Свитч',
  printer: 'Принтер',
  pc: 'ПК',
  other: 'Прочее',
}

/** Порядок значений — как в `shared/enums.py::DeviceType`. */
export const DEVICE_TYPE_OPTIONS = ['camera', 'switch', 'printer', 'pc', 'other'] as const

export const CHECK_TYPE_LABELS: Record<string, string> = {
  icmp: 'ping (ICMP)',
  tcp: 'TCP-порт',
}

/** Статусы устройства: серый `unknown` — «нет данных с кассы» (§4.11). */
export const DEVICE_STATUS_LABELS: Record<string, string> = {
  online: 'онлайн',
  offline: 'офлайн',
  unknown: 'нет данных',
}

export const DEVICE_STATUS_OPTIONS = ['online', 'offline', 'unknown'] as const

export function deviceTypeLabel(value: string | null): string {
  if (!value) return '—'
  return DEVICE_TYPE_LABELS[value] ?? value
}

export function checkTypeLabel(checkType: string, port: number | null): string {
  const label = CHECK_TYPE_LABELS[checkType] ?? checkType
  return checkType === 'tcp' && port ? `${label}:${port}` : label
}

export function deviceStatusLabel(status: string | null): string {
  if (!status) return DEVICE_STATUS_LABELS.unknown
  return DEVICE_STATUS_LABELS[status] ?? status
}

/** Цвет статуса: «онлайн» зелёный, «офлайн» красный, «нет данных» серый (§11.7). */
export function deviceStatusSeverity(status: string | null): 'success' | 'danger' | 'secondary' {
  if (status === 'online') return 'success'
  if (status === 'offline') return 'danger'
  return 'secondary'
}

/** Период автообновления таблицы `/devices` (§11.1: ~10–15 с). */
export const DEVICES_POLL_INTERVAL_MS = 15_000

export const useDevicesStore = defineStore('devices', () => {
  const matrix = ref<DevicesMatrix | null>(null)
  const isLoading = ref(false)
  const errorMessage = ref('')
  const lastLoadedAt = ref<Date | null>(null)

  // Фильтры страницы `/devices` (§11.7): магазин, кластер, тип, статус, поиск
  const storeId = ref<number | null>(null)
  const cluster = ref('')
  const deviceType = ref('')
  const status = ref<DeviceStatusFilter>('all')
  const search = ref('')
  const includeInactive = ref(true)

  const filters = computed<DeviceListFilters>(() => ({
    storeId: storeId.value,
    cluster: cluster.value,
    deviceType: deviceType.value,
    status: status.value,
    search: search.value,
    includeInactive: includeInactive.value,
  }))

  const rows = computed<DeviceItem[]>(() => matrix.value?.rows ?? [])
  const total = computed(() => matrix.value?.total ?? 0)
  const online = computed(() => matrix.value?.online ?? 0)
  const offline = computed(() => matrix.value?.offline ?? 0)
  const unknown = computed(() => matrix.value?.unknown ?? 0)
  const storesCount = computed(() => matrix.value?.stores ?? 0)
  const windowSec = computed(() => matrix.value?.window_sec ?? 180)

  const clusters = computed(() =>
    Array.from(new Set(rows.value.map((row) => row.cluster_name ?? ''))).filter(Boolean).sort(),
  )
  const deviceTypes = computed(() =>
    Array.from(new Set(rows.value.map((row) => row.device_type ?? ''))).filter(Boolean).sort(),
  )

  /** Перечитать сводку; при ошибке показанный список не затирается. */
  async function load(): Promise<void> {
    isLoading.value = true
    try {
      matrix.value = await devicesApi.matrix(filters.value)
      lastLoadedAt.value = new Date()
      errorMessage.value = ''
    } catch (error) {
      errorMessage.value =
        error instanceof ApiError ? error.message : 'Не удалось получить список оборудования'
    } finally {
      isLoading.value = false
    }
  }

  /** «Проверить сейчас» по магазину или по отдельным строкам (§11.7). */
  async function check(
    targetStoreId: number,
    deviceIds: number[] = [],
  ): Promise<DeviceCheckResponse | null> {
    try {
      const response = await devicesApi.check(targetStoreId, deviceIds)
      await load()
      errorMessage.value = ''
      return response
    } catch (error) {
      errorMessage.value = error instanceof ApiError ? error.message : 'Проверка не выполнена'
      return null
    }
  }

  async function create(payload: DeviceCreatePayload, targetStoreId: number): Promise<void> {
    await devicesApi.create(targetStoreId, payload)
    await load()
  }

  async function update(
    targetStoreId: number,
    deviceId: number,
    payload: DeviceUpdatePayload,
  ): Promise<void> {
    await devicesApi.update(targetStoreId, deviceId, payload)
    await load()
  }

  /** «Отключить из опроса» — мягкое удаление (`is_active=false`, §11.7). */
  async function deactivate(targetStoreId: number, deviceId: number): Promise<void> {
    await devicesApi.deactivate(targetStoreId, deviceId)
    await load()
  }

  /** Вернуть устройство в опрос (`PUT ... {is_active: true}`). */
  async function restore(targetStoreId: number, deviceId: number): Promise<void> {
    await devicesApi.update(targetStoreId, deviceId, { is_active: true })
    await load()
  }

  /**
   * Э10: удалить устройство из списка насовсем (`.../purge`).
   *
   * Это не «Отключить из опроса»: строка `store_devices` исчезает вместе с
   * историей опроса, а магазин получает агенту новый список устройств (§7.6).
   */
  async function purge(targetStoreId: number, deviceId: number): Promise<void> {
    await devicesApi.purge(targetStoreId, deviceId)
    await load()
  }

  let timer: ReturnType<typeof setInterval> | null = null

  /** Автообновление таблицы (идемпотентно) — включено, пока открыта страница. */
  function startPolling(intervalMs: number = DEVICES_POLL_INTERVAL_MS): void {
    stopPolling()
    void load()
    timer = setInterval(() => {
      void load()
    }, intervalMs)
  }

  function stopPolling(): void {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }

  return {
    matrix,
    isLoading,
    errorMessage,
    lastLoadedAt,
    storeId,
    cluster,
    deviceType,
    status,
    search,
    includeInactive,
    filters,
    rows,
    total,
    online,
    offline,
    unknown,
    storesCount,
    windowSec,
    clusters,
    deviceTypes,
    load,
    check,
    create,
    update,
    deactivate,
    restore,
    purge,
    startPolling,
    stopPolling,
  }
})
