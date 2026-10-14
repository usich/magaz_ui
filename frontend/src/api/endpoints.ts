/** Точки API: вход администратора (§5.2.2), системная информация, настройки, аудит. */

import { API_BASE, api } from './client'
import type {
  ActionPayload,
  ActionResponse,
  AdminCreatePayload,
  AdminUpdatePayload,
  AdminUser,
  AgentConfigBlock,
  AuditEntry,
  BulkActionPayload,
  BulkActionResponse,
  CancelNotificationResponse,
  DeviceCheckResponse,
  DeviceCreatePayload,
  DeviceItem,
  DeviceListFilters,
  DeviceUpdatePayload,
  DevicesMatrix,
  FileMatrix,
  FileMatrixFilters,
  JobDetail,
  JobFilters,
  JobsPage,
  LoginPayload,
  NotificationCreatePayload,
  NotificationCreateResponse,
  NotificationFilters,
  NotificationList,
  NotificationReport,
  ServerHealth,
  SettingItem,
  StoreCreatePayload,
  StoreCreated,
  StoreHealth,
  StoreItem,
  StoreListFilters,
  StoreUpdatePayload,
  SystemInfo,
  TokenRotateResult,
} from './types'

export const authApi = {
  /** `POST /api/v1/admin/auth/login` — выдаёт подписанную cookie сессии. */
  login: (payload: LoginPayload) => api.post<AdminUser>('/admin/auth/login', payload),

  /** `POST /api/v1/admin/auth/logout` — сбрасывает cookie. */
  logout: () => api.post<{ ok: boolean }>('/admin/auth/logout'),

  /**
   * `GET /api/v1/admin/auth/me` — текущий администратор.
   * `401` здесь означает «не вошёл», поэтому обработчик 401 не дёргаем.
   */
  me: () =>
    api.get<AdminUser>('/admin/auth/me', {
      skipUnauthorizedHandler: true,
    }),
}

export const systemApi = {
  /** Публичная проверка живости сервера (без авторизации). */
  health: () => api.get<ServerHealth>('/health', { skipUnauthorizedHandler: true }),

  /** Диагностика: версия, ревизия схемы, собрана ли SPA, счётчики. */
  info: () => api.get<SystemInfo>('/admin/system/info'),
}

export const settingsApi = {
  list: () => api.get<SettingItem[]>('/admin/settings'),

  update: (values: Record<string, string>) =>
    api.put<SettingItem[]>('/admin/settings', { values }),
}

export const auditApi = {
  list: (params: { limit?: number; offset?: number; actor?: string } = {}) => {
    const query = new URLSearchParams()
    if (params.limit !== undefined) query.set('limit', String(params.limit))
    if (params.offset !== undefined) query.set('offset', String(params.offset))
    if (params.actor) query.set('actor', params.actor)
    const suffix = query.toString() ? `?${query}` : ''
    return api.get<AuditEntry[]>(`/admin/audit${suffix}`)
  },
}

/**
 * Администраторы дашборда (`/api/v1/admin/admins`, Д-8, Э6).
 *
 * Хеш пароля не приходит ни в одном ответе: сервер отдаёт только `AdminUser`.
 * Запреты («себя» и «последнего действующего» трогать нельзя) живут на сервере
 * и приходят как `403`/`409` — UI показывает текст из ответа как есть.
 */
export const adminsApi = {
  list: () => api.get<AdminUser[]>('/admin/admins'),

  create: (payload: AdminCreatePayload) => api.post<AdminUser>('/admin/admins', payload),

  /** Смена пароля и/или блокировка (`{is_active: false}`) — §11.6. */
  update: (id: number, payload: AdminUpdatePayload) =>
    api.put<AdminUser>(`/admin/admins/${id}`, payload),

  /** Удаление учётной записи; история действий остаётся в аудите. */
  remove: (id: number) => api.delete<{ ok: boolean }>(`/admin/admins/${id}`),
}

/**
 * Магазины (`/api/v1/admin/stores`, §5.2.2, §11.2): CRUD, токен агента,
 * учётные данные 1С (П-1) и кнопка «Проверить сейчас» (Д-3).
 */
export const storesApi = {
  list: (filters: StoreListFilters = {}) => {
    const query = new URLSearchParams()
    if (filters.search?.trim()) query.set('search', filters.search.trim())
    if (filters.cluster?.trim()) query.set('cluster', filters.cluster.trim())
    if (filters.status && filters.status !== 'all') query.set('status', filters.status)
    if (filters.includeInactive) query.set('include_inactive', 'true')
    const suffix = query.toString() ? `?${query}` : ''
    return api.get<StoreItem[]>(`/admin/stores${suffix}`)
  },

  /** Создание магазина: сервер выдаёт токен и возвращает блок конфига агента. */
  create: (payload: StoreCreatePayload) => api.post<StoreCreated>('/admin/stores', payload),

  get: (id: number) => api.get<StoreItem>(`/admin/stores/${id}`),

  update: (id: number, payload: StoreUpdatePayload) =>
    api.put<StoreItem>(`/admin/stores/${id}`, payload),

  /** Мягкое удаление: `is_active=false`, магазин уходит из мониторинга. */
  remove: (id: number) => api.delete<StoreItem>(`/admin/stores/${id}`),

  /** Перевыпуск токена: агент отвечает `401`, пока его не перенастроят. */
  rotateToken: (id: number) => api.post<TokenRotateResult>(`/admin/stores/${id}/token/rotate`),

  /** Учётные данные 1С: `password` — задать, `clearPassword` — очистить (П-1). */
  setCredentials: (
    id: number,
    payload: { user_name_1c?: string; password?: string; clear_password?: boolean },
  ) => api.put<StoreItem>(`/admin/stores/${id}/credentials`, payload),

  /** «Проверить сейчас»: живой `GET /health` агента (Д-3). */
  check: (id: number) => api.post<StoreHealth>(`/admin/stores/${id}/check`),

  /**
   * `GET /admin/stores/{id}/agent-config` — данные `agent-config.json` (§11.2).
   *
   * Тот же блок, что и в ответе на создание, но доступен в любой момент: кассу
   * переустанавливают, токен перевыпустили — файл можно скачать заново.
   */
  agentConfig: (id: number) => api.get<AgentConfigBlock>(`/admin/stores/${id}/agent-config`),
}

/**
 * Операции над магазинами (Э4, §5.2.2, §11.4).
 *
 * Одиночное действие и массовое — разные точки контракта: первая адресуется
 * магазину, вторая выбирает получателей (`store_ids` → `cluster` → `all`).
 * Обе отвечают `202`: сервер не держит HTTP-запрос, пока касса обновляется, —
 * задачу забирает диспетчер.
 */
export const actionsApi = {
  /** `POST /admin/stores/{id}/actions` — операция для одного магазина. */
  run: (storeId: number, payload: ActionPayload) =>
    api.post<ActionResponse>(`/admin/stores/${storeId}/actions`, payload),

  /** `POST /admin/bulk/actions` — операция для выбранных магазинов, кластера или всех. */
  runBulk: (payload: BulkActionPayload) => api.post<BulkActionResponse>('/admin/bulk/actions', payload),
}

/** История задач и журналы (§5.2.2, §11.6). */
export const jobsApi = {
  list: (filters: JobFilters = {}) => {
    const query = new URLSearchParams()
    if (filters.storeId) query.set('store_id', String(filters.storeId))
    if (filters.kind) query.set('kind', filters.kind)
    if (filters.status) query.set('status', filters.status)
    if (filters.activeOnly) query.set('active_only', 'true')
    if (filters.limit !== undefined) query.set('limit', String(filters.limit))
    if (filters.offset !== undefined) query.set('offset', String(filters.offset))
    const suffix = query.toString() ? `?${query}` : ''
    return api.get<JobsPage>(`/admin/jobs${suffix}`)
  },

  /** Карточка задачи с полным журналом (`/jobs/{id}`, §11.6). */
  get: (id: number) => api.get<JobDetail>(`/admin/jobs/${id}`),
}

/** Матрица файлов обновления (§11.3, Q52). */
export const filesApi = {
  matrix: (filters: FileMatrixFilters = {}) => {
    const query = new URLSearchParams()
    if (filters.filename?.trim()) query.set('filename', filters.filename.trim())
    if (filters.cluster?.trim()) query.set('cluster', filters.cluster.trim())
    if (filters.includeInactive) query.set('include_inactive', 'true')
    const suffix = query.toString() ? `?${query}` : ''
    return api.get<FileMatrix>(`/admin/files/matrix${suffix}`)
  },
}

/**
 * Оборудование подсети (§11.7, Д-1, Э4Б).
 *
 * Подсеть опрашивает касса, поэтому «Проверить сейчас» — это запрос сервера к
 * агенту (`POST /devices/check-now`), а не скан сети с сервера (§7.6).
 * Сводная таблица `/devices` берётся из `/admin/devices/matrix`: строки
 * «магазин × устройство» и те же счётчики, что в плитке главной.
 */
export const devicesApi = {
  /** `GET /admin/stores/{id}/devices` — список оборудования магазина (карточка, §11.2). */
  list: (storeId: number, includeInactive = true) => {
    const suffix = includeInactive ? '?include_inactive=true' : ''
    return api.get<DeviceItem[]>(`/admin/stores/${storeId}/devices${suffix}`)
  },

  /** `POST /admin/stores/{id}/devices` — «Добавить устройство» (§11.7). */
  create: (storeId: number, payload: DeviceCreatePayload) =>
    api.post<DeviceItem>(`/admin/stores/${storeId}/devices`, payload),

  /** `PUT /admin/stores/{id}/devices/{device_id}` — правка карточки устройства. */
  update: (storeId: number, deviceId: number, payload: DeviceUpdatePayload) =>
    api.put<DeviceItem>(`/admin/stores/${storeId}/devices/${deviceId}`, payload),

  /** `DELETE /admin/stores/{id}/devices/{device_id}` — мягкое «Отключить из опроса». */
  deactivate: (storeId: number, deviceId: number) =>
    api.delete<DeviceItem>(`/admin/stores/${storeId}/devices/${deviceId}`),

  /** `POST /admin/stores/{id}/devices/check` — «Проверить сейчас» (пусто — все устройства). */
  check: (storeId: number, deviceIds: number[] = []) =>
    api.post<DeviceCheckResponse>(`/admin/stores/${storeId}/devices/check`, {
      device_ids: deviceIds,
    }),

  /** `GET /admin/devices/matrix` — сводная таблица `магазин × устройство` (§11.7). */
  matrix: (filters: DeviceListFilters = {}) => {
    const query = new URLSearchParams()
    if (filters.storeId) query.set('store_id', String(filters.storeId))
    if (filters.cluster?.trim()) query.set('cluster', filters.cluster.trim())
    if (filters.deviceType?.trim()) query.set('device_type', filters.deviceType.trim())
    if (filters.status && filters.status !== 'all') query.set('status', filters.status)
    if (filters.search?.trim()) query.set('search', filters.search.trim())
    if (filters.includeInactive === false) query.set('include_inactive', 'false')
    const suffix = query.toString() ? `?${query}` : ''
    return api.get<DevicesMatrix>(`/admin/devices/matrix${suffix}`)
  },
}

/**
 * Уведомления пользователям (§11.5, ФН3, Э5).
 *
 * Создание и рассылка — один запрос: сервер фиксирует адресацию и ставит задачи
 * `notify`, а доставку выполняет диспетчер (§11.4). Отчёт «X из Y» и CSV
 * читаются отдельно, поэтому список не «утяжеляется» сессиями.
 */
export const notificationsApi = {
  list: (filters: NotificationFilters = {}) => {
    const query = new URLSearchParams()
    if (filters.status && filters.status !== 'all') query.set('status', filters.status)
    if (filters.severity && filters.severity !== 'all') query.set('severity', filters.severity)
    if (filters.search?.trim()) query.set('search', filters.search.trim())
    if (filters.storeId) query.set('store_id', String(filters.storeId))
    if (filters.limit !== undefined) query.set('limit', String(filters.limit))
    const suffix = query.toString() ? `?${query}` : ''
    return api.get<NotificationList>(`/admin/notifications${suffix}`)
  },

  /** Создать и разослать: адресация «все / кластер / список магазинов» (§11.5). */
  create: (payload: NotificationCreatePayload) =>
    api.post<NotificationCreateResponse>('/admin/notifications', payload),

  /** Отчёт по магазинам и сессиям (§11.5): кто ознакомлен, кто отложил, кто ждёт. */
  get: (id: number) => api.get<NotificationReport>(`/admin/notifications/${id}`),

  /** Остановить рассылку: показы прекращаются, отчёт сохраняется (§11.5). */
  cancel: (id: number) =>
    api.post<CancelNotificationResponse>(`/admin/notifications/${id}/cancel`),

  /**
   * Ссылка на CSV-отчёт (§11.5): файл скачивает браузер, а cookie сессии
   * отправляется автоматически — вручную скачивать его через `fetch` не нужно.
   */
  reportCsvUrl: (id: number) =>
    `${API_BASE}/admin/notifications/${id}/report?format=csv`,
}

