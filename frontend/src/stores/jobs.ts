/**
 * Стор истории операций (§11.6, Э4).
 *
 * Задачи живут на сервере: страница `/jobs` только читает их и обновляет
 * polling-ом, как и главная (§11.1). Никаких «своих» статусов стор не выводит —
 * их считает сервер (`queued`/`sent`/`running`/`success`/`failed`/…), поэтому UI
 * и `agent-state.json` кассы не могут разойтись.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError } from '@/api/client'
import { jobsApi } from '@/api/endpoints'
import type { JobDetail, JobFilters, JobItem } from '@/api/types'

/** Период автообновления списка задач, мс (§11.1: «каждые ~10 с») */
export const JOBS_POLL_INTERVAL_MS = 10_000

/** Человеческие названия операций (§4.3 `kind`). */
export const JOB_KIND_LABELS: Record<string, string> = {
  check_files: 'Проверка файлов',
  download_file: 'Скачивание файла',
  run_merge: 'Слияние конфигурации',
  run_update_db: 'Обновление БД',
  send_message: 'Сообщение пользователям',
  notify: 'Уведомление',
  notify_cancel: 'Отзыв уведомления',
}

/** Подписи статусов (§4.3). */
export const JOB_STATUS_LABELS: Record<string, string> = {
  queued: 'в очереди',
  sent: 'отправлена',
  running: 'выполняется',
  success: 'успех',
  failed: 'ошибка',
  timeout: 'таймаут',
  error: 'не выполнена',
  canceled: 'отменена',
}

/** Severity PrimeVue-тега для статуса задачи. */
export function jobStatusSeverity(status: string): 'success' | 'info' | 'warn' | 'danger' | 'secondary' {
  if (status === 'success') return 'success'
  if (status === 'failed' || status === 'error' || status === 'timeout') return 'danger'
  if (status === 'running') return 'warn'
  if (status === 'canceled') return 'secondary'
  return 'info'
}

export const useJobsStore = defineStore('jobs', () => {
  const items = ref<JobItem[]>([])
  const counts = ref<Record<string, number>>({})
  const total = ref(0)
  const active = ref(0)
  const isLoading = ref(false)
  const errorMessage = ref('')
  const lastLoadedAt = ref<Date | null>(null)

  // Фильтры страницы `/jobs` (§11.6): магазин, тип, статус, «только в работе»
  const storeId = ref<number | null>(null)
  const kind = ref('')
  const status = ref('')
  const activeOnly = ref(false)

  const detail = ref<JobDetail | null>(null)
  const isDetailLoading = ref(false)

  // Выбор задач для удаления (§11.6, Э10): id выбранных строк
  const selectedIds = ref<number[]>([])
  const isDeleting = ref(false)

  const filters = computed<JobFilters>(() => ({
    storeId: storeId.value,
    kind: kind.value,
    status: status.value,
    activeOnly: activeOnly.value,
    limit: 100,
  }))

  /** Переключить выбор одной задачи (или снять при повторном клике). */
  function toggleSelect(id: number): void {
    const index = selectedIds.value.indexOf(id)
    if (index === -1) selectedIds.value.push(id)
    else selectedIds.value.splice(index, 1)
  }

  /** Выбрать / снять все видимые на странице задачи. */
  function selectAll(): void {
    const visible = items.value.map((item) => item.id)
    const allVisibleSelected = visible.every((id) => selectedIds.value.includes(id))
    selectedIds.value = allVisibleSelected
      ? selectedIds.value.filter((id) => !visible.includes(id))
      : [...new Set([...selectedIds.value, ...visible])]
  }

  function clearSelection(): void {
    selectedIds.value = []
  }

  /** Удалить одну задачу и перечитать список. */
  async function removeOne(id: number): Promise<void> {
    isDeleting.value = true
    try {
      await jobsApi.remove(id)
      selectedIds.value = selectedIds.value.filter((item) => item !== id)
      await load()
    } catch (error) {
      errorMessage.value =
        error instanceof ApiError ? error.message : 'Не удалось удалить задачу'
    } finally {
      isDeleting.value = false
    }
  }

  /** Удалить выбранные задачи и перечитать список. */
  async function removeSelected(): Promise<void> {
    const ids = [...selectedIds.value]
    if (!ids.length) return
    isDeleting.value = true
    try {
      await jobsApi.removeBulk(ids)
      selectedIds.value = []
      await load()
    } catch (error) {
      errorMessage.value =
        error instanceof ApiError ? error.message : 'Не удалось удалить выбранные задачи'
    } finally {
      isDeleting.value = false
    }
  }

  async function load(): Promise<void> {
    isLoading.value = true
    try {
      const page = await jobsApi.list(filters.value)
      items.value = page.items
      counts.value = page.counts
      total.value = page.total
      active.value = page.active
      lastLoadedAt.value = new Date()
      errorMessage.value = ''
    } catch (error) {
      errorMessage.value =
        error instanceof ApiError ? error.message : 'Не удалось получить историю задач'
    } finally {
      isLoading.value = false
    }
  }

  async function loadDetail(id: number): Promise<JobDetail | null> {
    isDetailLoading.value = true
    try {
      detail.value = await jobsApi.get(id)
      errorMessage.value = ''
      return detail.value
    } catch (error) {
      detail.value = null
      errorMessage.value = error instanceof ApiError ? error.message : 'Задача не найдена'
      return null
    } finally {
      isDetailLoading.value = false
    }
  }

  let timer: ReturnType<typeof setInterval> | null = null

  /** Автообновление (идемпотентно): включено, пока открыта страница. */
  function startPolling(intervalMs: number = JOBS_POLL_INTERVAL_MS): void {
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
    counts,
    total,
    active,
    isLoading,
    errorMessage,
    lastLoadedAt,
    storeId,
    kind,
    status,
    activeOnly,
    filters,
    detail,
    isDetailLoading,
    selectedIds,
    isDeleting,
    load,
    loadDetail,
    toggleSelect,
    selectAll,
    clearSelection,
    removeOne,
    removeSelected,
    startPolling,
    stopPolling,
  }
})