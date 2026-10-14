/**
 * Стор уведомлений (§11.5, ФН3, Э5).
 *
 * Список и отчёт живут в одном сторе: страница `/notifications` показывает
 * прогресс «ознакомлены X из Y», а карточка `/notifications/{id}` — тот же
 * прогресс по каждой сессии. Кэш ничего не досчитывает: статус рассылки,
 * процент и «ждут ознакомления» приходят с сервера (§4.8), поэтому отчёт в UI не
 * может разойтись с фактом.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError } from '@/api/client'
import { notificationsApi } from '@/api/endpoints'
import type {
  NotificationCreatePayload,
  NotificationCreateResponse,
  NotificationFilters,
  NotificationItem,
  NotificationList,
  NotificationReport,
} from '@/api/types'

/** Подписи уровней важности (§4.6). */
export const SEVERITY_LABELS: Record<string, string> = {
  info: 'информация',
  important: 'важно',
  critical: 'критично',
}

export const SEVERITY_OPTIONS = ['info', 'important', 'critical'] as const

/** Цвет уровня в таблице: `critical` красный, `important` жёлтый, `info` серый. */
export function severityTone(severity: string): 'warn' | 'danger' | 'secondary' {
  if (severity === 'critical') return 'danger'
  if (severity === 'important') return 'warn'
  return 'secondary'
}

/** Подписи статусов рассылки (§11.5): работа / завершено / отменено. */
export const STATE_LABELS: Record<string, string> = {
  active: 'в работе',
  done: 'завершено',
  canceled: 'отменено',
}

export const STATE_OPTIONS = ['all', 'active', 'done', 'canceled'] as const

/** Подписи статусов сессии (§4.8): отложенное выделяется отдельно (П-2). */
export const RECEIPT_LABELS: Record<string, string> = {
  pending: 'ожидание',
  shown: 'показано',
  acknowledged: 'ознакомлен',
  snoozed: 'отложено',
  expired: 'просрочено',
}

export function receiptLabel(status: string | null): string {
  if (!status) return RECEIPT_LABELS.pending
  return RECEIPT_LABELS[status] ?? status
}

/** Цвет статуса сессии: зелёный — ознакомлен, жёлтый — отложено (П-2). */
export function receiptTone(status: string | null): 'success' | 'warn' | 'danger' | 'secondary' {
  if (status === 'acknowledged') return 'success'
  if (status === 'snoozed') return 'warn'
  if (status === 'expired') return 'danger'
  return 'secondary'
}

/** Охват рассылки человеческим языком (§11.5). */
export function targetTypeLabel(item: NotificationItem): string {
  if (item.target_type === 'all') return 'все магазины'
  if (item.target_type === 'cluster') return `кластер ${item.target_value ?? ''}`.trim()
  return item.store_names.length ? item.store_names.join(', ') : 'выбранные магазины'
}

export function severityLabel(value: string | null): string {
  if (!value) return SEVERITY_LABELS.info
  return SEVERITY_LABELS[value] ?? value
}

export function stateLabel(value: string | null): string {
  if (!value) return STATE_LABELS.active
  return STATE_LABELS[value] ?? value
}

export const useNotificationsStore = defineStore('notifications', () => {
  const list = ref<NotificationList | null>(null)
  const report = ref<NotificationReport | null>(null)
  const isLoading = ref(false)
  const isSaving = ref(false)
  const errorMessage = ref('')
  const lastLoadedAt = ref<Date | null>(null)

  // Фильтры страницы `/notifications` (§11.5): статус, уровень, поиск, магазин
  const status = ref<NotificationFilters['status']>('all')
  const severity = ref<NotificationFilters['severity']>('all')
  const search = ref('')
  const storeId = ref<number | null>(null)

  const filters = computed<NotificationFilters>(() => ({
    status: status.value,
    severity: severity.value,
    search: search.value,
    storeId: storeId.value,
  }))

  const items = computed<NotificationItem[]>(() => list.value?.items ?? [])
  const total = computed(() => list.value?.total ?? 0)
  const counts = computed(
    () => list.value?.counts ?? { active: 0, done: 0, canceled: 0 },
  )
  const pendingReceipts = computed(() => list.value?.pending_receipts ?? 0)
  /** Интервал возврата отложенного — настройка сервера (§4.9, П-2). */
  const snoozeMin = computed(() => list.value?.snooze_min ?? 15)

  /** Перечитать список; при ошибке показанные строки не затираются. */
  async function load(): Promise<void> {
    isLoading.value = true
    try {
      list.value = await notificationsApi.list(filters.value)
      lastLoadedAt.value = new Date()
      errorMessage.value = ''
    } catch (error) {
      errorMessage.value =
        error instanceof ApiError ? error.message : 'Не удалось получить уведомления'
    } finally {
      isLoading.value = false
    }
  }

  /** Отчёт по уведомлению: магазины → сессии (§11.5). */
  async function loadReport(id: number): Promise<NotificationReport | null> {
    isLoading.value = true
    try {
      report.value = await notificationsApi.get(id)
      errorMessage.value = ''
      return report.value
    } catch (error) {
      report.value = null
      errorMessage.value = error instanceof ApiError ? error.message : 'Не удалось получить отчёт'
      return null
    } finally {
      isLoading.value = false
    }
  }

  /** Создать и разослать уведомление; `null` — ошибку показывает UI (§11.5). */
  async function create(
    payload: NotificationCreatePayload,
  ): Promise<NotificationCreateResponse | null> {
    isSaving.value = true
    try {
      const response = await notificationsApi.create(payload)
      await load()
      errorMessage.value = ''
      return response
    } catch (error) {
      errorMessage.value = error instanceof ApiError ? error.message : 'Уведомление не создано'
      return null
    } finally {
      isSaving.value = false
    }
  }

  /** «Отменить»: показы прекращаются, отчёт сохраняется (§11.5). */
  async function cancel(id: number): Promise<boolean> {
    try {
      await notificationsApi.cancel(id)
      await load()
      if (report.value?.notification.id === id) {
        await loadReport(id)
      }
      errorMessage.value = ''
      return true
    } catch (error) {
      errorMessage.value = error instanceof ApiError ? error.message : 'Рассылка не отменена'
      return false
    }
  }

  /** Ссылка на CSV-отчёт: скачивание делает браузер вместе с cookie сессии (§11.5). */
  function csvUrl(id: number): string {
    return notificationsApi.reportCsvUrl(id)
  }

  return {
    list,
    report,
    isLoading,
    isSaving,
    errorMessage,
    lastLoadedAt,
    status,
    severity,
    search,
    storeId,
    filters,
    items,
    total,
    counts,
    pendingReceipts,
    snoozeMin,
    load,
    loadReport,
    create,
    cancel,
    csvUrl,
  }
})
