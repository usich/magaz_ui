/**
 * Стор магазинов (§4.1, §11.2, Э2).
 *
 * Страница `/stores` и главная работают с одним сторем: список обновляется
 * polling-ом, как описано в §11.1. Признак «онлайн» считает сервер по окну
 * `online_window_sec` (§3.3), поэтому кэш не хранит собственных выводов о
 * статусе — только то, что вернул API.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError } from '@/api/client'
import { storesApi } from '@/api/endpoints'
import type {
  StoreCreatePayload,
  StoreCreated,
  StoreHealth,
  StoreItem,
  StoreListFilters,
  StoreStatusFilter,
  StoreUpdatePayload,
  TokenRotateResult,
} from '@/api/types'

/** Период автообновления списка магазинов (§11.1: ~10 с) */
export const STORES_POLL_INTERVAL_MS = 10_000

export const useStoresStore = defineStore('stores', () => {
  const items = ref<StoreItem[]>([])
  const isLoading = ref(false)
  const errorMessage = ref('')
  const lastLoadedAt = ref<Date | null>(null)

  // Фильтры страницы `/stores` (§11.2): поиск, кластер, статус, неактивные
  const search = ref('')
  const cluster = ref('')
  const status = ref<StoreStatusFilter>('all')
  const includeInactive = ref(false)

  const filters = computed<StoreListFilters>(() => ({
    search: search.value,
    cluster: cluster.value,
    status: status.value,
    includeInactive: includeInactive.value,
  }))

  const clusters = computed(() =>
    Array.from(new Set(items.value.map((item) => item.cluster_name))).sort(),
  )
  const onlineCount = computed(() => items.value.filter((item) => item.is_online).length)
  const offlineCount = computed(() => items.value.length - onlineCount.value)

  function byId(id: number): StoreItem | null {
    return items.value.find((item) => item.id === id) ?? null
  }

  function upsert(store: StoreItem): void {
    const index = items.value.findIndex((item) => item.id === store.id)
    if (index === -1) {
      items.value = [...items.value, store]
      return
    }
    items.value = items.value.map((item) => (item.id === store.id ? store : item))
  }

  /** Перечитать список с текущими фильтрами. */
  async function load(): Promise<void> {
    isLoading.value = true
    try {
      items.value = await storesApi.list(filters.value)
      lastLoadedAt.value = new Date()
      errorMessage.value = ''
    } catch (error) {
      errorMessage.value =
        error instanceof ApiError ? error.message : 'Не удалось получить список магазинов'
    } finally {
      isLoading.value = false
    }
  }

  /** Создать магазин: возвращаем ответ вместе с блоком конфига агента (§11.2). */
  async function create(payload: StoreCreatePayload): Promise<StoreCreated> {
    const created = await storesApi.create(payload)
    await load()
    return created
  }

  async function update(id: number, payload: StoreUpdatePayload): Promise<StoreItem> {
    const updated = await storesApi.update(id, payload)
    upsert(updated)
    return updated
  }

  /** Мягкое удаление: из списка активных магазин уходит сразу. */
  async function remove(id: number): Promise<StoreItem> {
    const updated = await storesApi.remove(id)
    if (includeInactive.value) {
      upsert(updated)
    } else {
      items.value = items.value.filter((item) => item.id !== id)
    }
    return updated
  }

  async function rotateToken(id: number): Promise<TokenRotateResult> {
    return storesApi.rotateToken(id)
  }

  /** «Проверить сейчас» (Д-3): живой `GET /health` + перечитка карточки. */
  async function checkHealth(id: number): Promise<StoreHealth> {
    const health = await storesApi.check(id)
    await load()
    return health
  }

  async function setCredentials(
    id: number,
    payload: { user_name_1c?: string; password?: string; clear_password?: boolean },
  ): Promise<StoreItem> {
    const updated = await storesApi.setCredentials(id, payload)
    upsert(updated)
    return updated
  }

  let timer: ReturnType<typeof setInterval> | null = null

  /** Автообновление (идемпотентно) — включено, пока открыта страница. */
  function startPolling(intervalMs: number = STORES_POLL_INTERVAL_MS): void {
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
    items,
    isLoading,
    errorMessage,
    lastLoadedAt,
    search,
    cluster,
    status,
    includeInactive,
    filters,
    clusters,
    onlineCount,
    offlineCount,
    byId,
    load,
    create,
    update,
    remove,
    rotateToken,
    checkHealth,
    setCredentials,
    startPolling,
    stopPolling,
  }
})
