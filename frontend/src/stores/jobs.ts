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

  const filters = computed<JobFilters>(() => ({
    storeId: storeId.value,
    kind: kind.value,
    status: status.value,
    activeOnly: activeOnly.value,
    limit: 100,
  }))

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
    load,
    loadDetail,
    startPolling,
    stopPolling,
  }
})