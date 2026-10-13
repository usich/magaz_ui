/** Типы ответов админского API (§5.2.2) — состав Э1. */

export interface AdminUser {
  id: number
  username: string
  is_active: boolean
  last_login_at: string | null
}

export interface LoginPayload {
  username: string
  password: string
}

export interface DatabaseInfo {
  kind: string
  schema_version: string | null
}

export interface SpaInfo {
  dist_dir: string
  built: boolean
}

export interface SystemCounts {
  stores_total: number
  stores_active: number
  /** Э2: магазины, ответившие за окно `online_window_sec` (§3.3) */
  stores_online: number
  /** Э2: активные магазины без свежего ответа */
  stores_offline: number
  online_window_sec: number
  admins_total: number
  audit_events: number
}

export interface SystemInfo {
  app_name: string
  app_version: string
  database: DatabaseInfo
  spa: SpaInfo
  counts: SystemCounts
  settings_keys: number
}

export interface ServerHealth {
  status: string
  app_name: string
  app_version: string
}

export interface SettingItem {
  key: string
  value: string
  default: string
  is_default: boolean
  description: string
}

export interface AuditEntry {
  id: number
  actor: string | null
  action: string
  entity: string | null
  entity_id: string | null
  details: Record<string, unknown> | null
  ip_address: string | null
  created_at: string | null
}

/** Магазин в API (§4.1, §11.2). Токена агента и пароля здесь нет. */
export interface StoreItem {
  id: number
  name: string
  ip_address: string
  agent_port: number
  cluster_name: string
  database_name: string
  version_1c: string
  user_name_1c: string
  password_set: boolean
  credentials_version: number
  devices_version: number
  agent_version: string | null
  os_info: string | null
  last_seen_at: string | null
  is_online: boolean
  is_active: boolean
  comment: string | null
  created_at: string | null
  updated_at: string | null
}

/** Блок «что вписать в конфиг агента» (§11.2). */
export interface AgentConfigBlock {
  store_id: number
  server_url: string
  port: number
  agent_token: string
  config_path: string
  config_json: Record<string, unknown>
  notes: string[]
}

/** Ответ на создание магазина: карточка + данные `agent-config.json`. */
export interface StoreCreated extends StoreItem {
  agent_config: AgentConfigBlock
}

/** Тело `POST /stores` — колонки ТЗ плюс служебные поля. */
export interface StoreCreatePayload {
  name: string
  ip_address: string
  cluster_name: string
  database_name: string
  version_1c: string
  user_name_1c: string
  agent_port: number
  password?: string | null
  comment?: string | null
}

/** Тело `PUT /stores/{id}`: передаются только изменяемые поля. */
export type StoreUpdatePayload = Partial<Omit<StoreCreatePayload, 'password'>> & {
  is_active?: boolean
}

export interface TokenRotateResult {
  store_id: number
  agent_token: string
  warning: string
}

/** Живой ответ агента на «Проверить сейчас» (Д-3). */
export interface StoreHealth {
  store_id: number
  status: string
  checked_at: string
  latency_ms: number | null
  agent_version: string
  uptime_sec: number
  os: string | null
  work_dir: string | null
  base_1c_path: string | null
  busy: { merge: boolean; update: boolean }
  sessions: { user: string; sid: string }[]
  credentials_version: number
  devices_version: number
  credentials_push_required: boolean
  devices_push_required: boolean
}

export type StoreStatusFilter = 'all' | 'online' | 'offline'

export interface StoreListFilters {
  search?: string
  cluster?: string
  status?: StoreStatusFilter
  includeInactive?: boolean
}
