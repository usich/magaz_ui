/** Точки API: вход администратора (§5.2.2), системная информация, настройки, аудит. */

import { api } from './client'
import type {
  AdminUser,
  AuditEntry,
  LoginPayload,
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
}
