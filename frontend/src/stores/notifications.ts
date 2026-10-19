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
import { categoriesApi, notificationsApi } from '@/api/endpoints'
import type {
  NotificationCategory,
  NotificationCategoryPayload,
  NotificationCreatePayload,
  NotificationCreateResponse,
  NotificationFilters,
  NotificationImageUpload,
  NotificationItem,
  NotificationList,
  NotificationReport,
  NotificationSeverity,
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

// --- Форматированный текст и картинки (Э9, §4.6) ----------------------------

/** Метка картинки в теле: `magaz-image:<имя>` — адрес подставляет потребитель. */
export const IMAGE_SRC_PREFIX = 'magaz-image:'

/** Лимиты сервера (§4.6): 2 МБ на картинку и 10 картинок на уведомление. */
export const IMAGE_MAX_BYTES = 2 * 1024 * 1024
export const IMAGES_MAX_PER_NOTIFICATION = 10
export const IMAGE_CONTENT_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']

/**
 * Черновик картинки: файл выбран в редакторе, но на сервер ещё не ушёл (Э9).
 *
 * Картинка уезжает вместе с уведомлением (§7.4), поэтому до сохранения она живёт
 * здесь: в теле стоит метка `magaz-image:<имя>`, а `dataUrl` нужен редактору и
 * предпросмотру, чтобы картинка была видна до создания уведомления.
 */
export interface NotificationImageDraft {
  name: string
  contentType: string
  dataUrl: string
  sizeBytes: number
  /** Имя файла с машины администратора — для списка приложенных картинок. */
  sourceName: string
}

/** Имя картинки из метки `magaz-image:<имя>`; `null` — это не наша картинка. */
export function imageNameFromSrc(src: string | null | undefined): string | null {
  if (!src || !src.toLowerCase().startsWith(IMAGE_SRC_PREFIX)) return null
  const name = src.slice(IMAGE_SRC_PREFIX.length).trim()
  return /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(name) ? name : null
}

/** Имя файла для сервера: латиница, цифры, точка, дефис — как в `shared.richtext`. */
export function makeImageName(sourceName: string): string {
  const extension = (sourceName.split('.').pop() ?? '').toLowerCase()
  const safe = extension === 'jpeg' ? 'jpg' : extension
  const allowed = ['png', 'jpg', 'gif', 'webp']
  return `img-${Math.random().toString(16).slice(2, 10)}.${allowed.includes(safe) ? safe : 'png'}`
}

/** Размер картинки по-человечески: «2 МБ» — для подсказок и ошибок (Э9). */
export function sizeMb(bytes: number): string {
  return `${Math.round((bytes / (1024 * 1024)) * 10) / 10} МБ`
}

/**
 * Почему картинку нельзя приложить; `null` — можно (проверки сервера дублирует UI).
 *
 * Пустой `type` — не отказ: браузер не всегда знает тип (например, при вставке из
 * буфера). Тогда редактор определит тип по содержимому файла (Э9).
 */
export function imageFileError(file: { type?: string; size: number }): string | null {
  const type = (file.type ?? '').toLowerCase()
  if (type && !IMAGE_CONTENT_TYPES.includes(type)) {
    return 'Поддерживаются PNG, JPEG, GIF и WebP'
  }
  if (file.size > IMAGE_MAX_BYTES) {
    return `Картинка больше ${sizeMb(IMAGE_MAX_BYTES)}`
  }
  return null
}

/** Картинка для `POST /notifications`: имена уже стоят в теле метками (§7.4). */
export function imageUpload(draft: NotificationImageDraft): NotificationImageUpload {
  return { name: draft.name, data_base64: draft.dataUrl }
}

/**
 * Текстовая версия тела: в списке и отчёте показываем текст, а не разметку (Э9).
 *
 * Повторяет `shared.richtext.html_to_text` в объёме, который нужен экрану: абзацы
 * и списки — отдельными строками, картинка — подписью `alt`.
 */
export function htmlToText(value: string): string {
  const parsed = new DOMParser().parseFromString(value, 'text/html')
  parsed.querySelectorAll('script, style').forEach((node) => node.remove())
  parsed.querySelectorAll('br').forEach((node) => node.replaceWith('\n'))
  parsed.querySelectorAll('li').forEach((node) => node.prepend('• '))
  parsed
    .querySelectorAll('p, div, h1, h2, h3, h4, li, tr, blockquote, pre')
    .forEach((node) => node.append('\n'))
  parsed
    .querySelectorAll('img')
    .forEach((node) => node.replaceWith(` [${node.getAttribute('alt') || 'картинка'}] `))
  const text = parsed.body?.textContent ?? ''
  return text.replace(/[ \t]+\n/g, '\n').replace(/\n{2,}/g, '\n').trim()
}

/** Тело для списка и подсказок: у разметки — текст, у простого тела — как есть. */
export function bodyText(item: NotificationItem): string {
  return item.body_format === 'html' ? htmlToText(item.body) : item.body
}

/** Адрес картинки тела: SPA берёт админский эндпоинт, касса — свой кэш (§7.4). */
export function notificationImageUrl(notificationId: number | null, name: string): string {
  if (!notificationId) return ''
  return notificationsApi.imageUrl(notificationId, name)
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

  // --- Категории уведомлений (спека 1.22, §11.5) ---------------------------

  /** Активные категории: их предлагает форма создания уведомления. */
  const categories = ref<NotificationCategory[]>([])
  /** Все категории, включая отключённые: их показывает раздел «Категории». */
  const allCategories = ref<NotificationCategory[]>([])
  const isLoadingCategories = ref(false)

  /**
   * Палитра плашек (1.22, §11.5) — та же, что `notification_service.CATEGORY_COLORS`
   * на сервере: сервер отвергает чужой цвет (`422`), поэтому UI предлагает выбор
   * только из этого списка. Захардкожена осознанно: меняется вместе с сервером.
   */
  const CATEGORY_COLORS = [
    '#64748b',
    '#2563eb',
    '#0ea5e9',
    '#10b981',
    '#f59e0b',
    '#ef4444',
    '#8b5cf6',
    '#ec4899',
  ] as const

  /** Прочитать категории: `all` — вместе с отключёнными (для раздела «Категории»). */
  async function loadCategories(all = false): Promise<void> {
    isLoadingCategories.value = true
    try {
      const items = await categoriesApi.list(all)
      if (all) {
        allCategories.value = items
        categories.value = items.filter((item) => item.is_active)
      } else {
        categories.value = items
      }
      errorMessage.value = ''
    } catch (error) {
      errorMessage.value =
        error instanceof ApiError ? error.message : 'Не удалось получить категории'
    } finally {
      isLoadingCategories.value = false
    }
  }

  /** Завести категорию; `null` — ошибку (в том числе дубль имени) показывает UI. */
  async function createCategory(payload: NotificationCategoryPayload): Promise<boolean> {
    try {
      await categoriesApi.create(payload)
      await loadCategories(true)
      errorMessage.value = ''
      return true
    } catch (error) {
      errorMessage.value = error instanceof ApiError ? error.message : 'Категория не создана'
      return false
    }
  }

  /** Изменить категорию: имя, цвет, порядок или включить отключённую обратно. */
  async function updateCategory(
    id: number,
    payload: NotificationCategoryPayload,
  ): Promise<boolean> {
    try {
      await categoriesApi.update(id, payload)
      await loadCategories(true)
      errorMessage.value = ''
      return true
    } catch (error) {
      errorMessage.value = error instanceof ApiError ? error.message : 'Категория не изменена'
      return false
    }
  }

  /** Мягко отключить категорию: у уже созданных уведомлений она сохранится (§11.5). */
  async function removeCategory(id: number): Promise<boolean> {
    try {
      await categoriesApi.remove(id)
      await loadCategories(true)
      errorMessage.value = ''
      return true
    } catch (error) {
      errorMessage.value = error instanceof ApiError ? error.message : 'Категория не отключена'
      return false
    }
  }

  /**
   * Счётчики вкладок по важности (Э9, §11.5).
   *
   * Числа приходят в том же ответе, что и список, и считаются сервером по **всем**
   * уведомлениям: фильтр вкладки не должен менять цифры на соседних вкладках.
   */
  const severityCounts = computed<Record<string, number>>(() => list.value?.severity_counts ?? {})
  const unreadBySeverity = computed<Record<string, number>>(
    () => list.value?.unread_by_severity ?? {},
  )

  /** Вкладки «все / информация / важно / критично» со счётчиками и «ждут ознакомления». */
  const severityTabs = computed<
    { value: NotificationSeverity | 'all'; label: string; count: number; unread: number }[]
  >(() =>
    [
      { value: 'all', label: 'все', count: total.value, unread: 0 },
      ...SEVERITY_OPTIONS.map((item) => ({
        value: item,
        label: SEVERITY_LABELS[item],
        count: severityCounts.value[item] ?? 0,
        unread: unreadBySeverity.value[item] ?? 0,
      })),
    ],
  )

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

  /**
   * Э10: удалить уведомление целиком (адресаты, отметки, картинки, задачи).
   * Список перечитывается, а если открыт отчёт по этому уведомлению — он гасится:
   * показывать нечего, уведомления больше нет (§11.5).
   */
  async function remove(id: number): Promise<boolean> {
    try {
      await notificationsApi.remove(id)
      if (report.value?.notification.id === id) {
        report.value = null
      }
      await load()
      errorMessage.value = ''
      return true
    } catch (error) {
      errorMessage.value = error instanceof ApiError ? error.message : 'Уведомление не удалено'
      return false
    }
  }

  /** Э10: убрать картинку из тела уведомления — файл, запись и метку (§4.6). */
  async function removeImage(id: number, name: string): Promise<boolean> {
    try {
      await notificationsApi.removeImage(id, name)
      await loadReport(id)
      errorMessage.value = ''
      return true
    } catch (error) {
      errorMessage.value = error instanceof ApiError ? error.message : 'Картинка не удалена'
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
    severityCounts,
    unreadBySeverity,
    severityTabs,
    pendingReceipts,
    snoozeMin,
    categories,
    allCategories,
    isLoadingCategories,
    CATEGORY_COLORS,
    load,
    loadReport,
    create,
    cancel,
    remove,
    removeImage,
    loadCategories,
    createCategory,
    updateCategory,
    removeCategory,
    csvUrl,
  }
})
