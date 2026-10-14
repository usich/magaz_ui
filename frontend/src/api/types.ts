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

/** Тело `POST /admin/admins` (Д-8, Э6): логин и пароль нового администратора. */
export interface AdminCreatePayload {
  username: string
  password: string
}

/**
 * Тело `PUT /admin/admins/{id}` (Д-8, Э6): смена пароля и/или блокировка.
 * Оба поля необязательны, но хотя бы одно обязательно — сервер отвечает `422`.
 */
export interface AdminUpdatePayload {
  password?: string
  is_active?: boolean
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
  /** Э4: задачи в работе (queued/sent/running) — плитка «Задач в работе» */
  jobs_active: number
  /** Э4Б: устройств в списке магазинов — плитка «Устройства онлайн/всего» (Д-1) */
  devices_total: number
  devices_online: number
  /** Э4Б: окно «устройство онлайн» — своё, не `online_window_sec` (§4.11) */
  devices_window_sec: number
  /** Э5: сессии, которые ещё не нажали «Ознакомлен» — плитка «Ждут ознакомления» (§4.8, П-2) */
  notifications_pending: number
  /** Э5: интервал возврата отложенного уведомления — `notification_snooze_min` (§4.9) */
  notifications_snooze_min: number
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

/** Задача в истории операций (§4.3, §11.6). Секретов здесь нет по построению. */
export interface JobItem {
  id: number
  store_id: number
  store_name: string | null
  kind: string
  status: string
  params: Record<string, unknown> | null
  attempts: number
  last_error: string | null
  log_files: string[]
  created_by: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  last_message: string | null
  excerpt_preview: string | null
}

/** Строка журнала задачи (§4.4). */
export interface JobLogEntry {
  id: number
  level: string
  message: string
  created_at: string | null
}

export interface JobDetail extends JobItem {
  logs: JobLogEntry[]
}

/** Ответ `GET /jobs`: элементы, счётчики по статусам, всего и «в работе» (§11.6). */
export interface JobsPage {
  items: JobItem[]
  counts: Record<string, number>
  total: number
  active: number
}

export interface JobFilters {
  storeId?: number | null
  kind?: string
  status?: string
  activeOnly?: boolean
  limit?: number
  offset?: number
}

/** Строка матрицы файлов (§11.3): последний снимок проверки на магазин. */
export interface FileMatrixRow {
  store_id: number
  store_name: string
  ip_address: string
  cluster_name: string
  is_active: boolean
  is_online: boolean
  filename: string
  checked_at: string | null
  cf_exists: boolean
  cfe_exists: boolean
  xml_exists: boolean
  cf_size: number | null
  cfe_size: number | null
  xml_size: number | null
  cf_mtime: string | null
  cfe_mtime: string | null
  xml_mtime: string | null
  has_file: boolean
  checked: boolean
}

export interface FileMatrix {
  filename: string
  rows: FileMatrixRow[]
  total: number
  present: number
  missing_current: number
}

export interface FileMatrixFilters {
  filename?: string
  cluster?: string
  includeInactive?: boolean
}

/** Способ проверки устройства (§4.11, Д-1): ping либо TCP-connect. */
export type DeviceCheckType = 'icmp' | 'tcp'

/** Статус устройства: `unknown` — «нет данных с кассы», а не «нет связи» (§4.11). */
export type DeviceStatusValue = 'online' | 'offline' | 'unknown'

/** Фильтр статуса на странице `/devices` (§11.7). */
export type DeviceStatusFilter = 'all' | DeviceStatusValue

/**
 * Строка оборудования подсети (§4.11, §11.7, Д-1).
 *
 * `is_online` и `store_is_online` сервер считает на лету по окнам
 * `device_online_window_sec` и `online_window_sec` — они не хранятся в БД.
 */
export interface DeviceItem {
  id: number
  store_id: number
  store_name: string | null
  cluster_name: string | null
  store_is_active: boolean
  store_is_online: boolean
  name: string
  ip_address: string
  device_type: string | null
  check_type: DeviceCheckType
  check_port: number | null
  is_active: boolean
  last_status: DeviceStatusValue | null
  last_seen_at: string | null
  last_latency_ms: number | null
  last_error: string | null
  comment: string | null
  is_online: boolean
}

/** Фильтры таблицы `/devices` (§11.7): магазин, кластер, тип, статус, поиск. */
export interface DeviceListFilters {
  storeId?: number | null
  cluster?: string
  deviceType?: string
  status?: DeviceStatusFilter
  search?: string
  includeInactive?: boolean
}

/** Ответ `GET /admin/devices/matrix`: строки и счётчики страницы `/devices`. */
export interface DevicesMatrix {
  rows: DeviceItem[]
  total: number
  online: number
  offline: number
  unknown: number
  stores: number
  window_sec: number
}

/** Результат проверки одного адреса (ответ кассы на `/devices/check-now`). */
export interface DeviceCheckResult {
  ip: string
  status: DeviceStatusValue
  latency_ms: number | null
  error: string | null
}

/** Ответ «Проверить сейчас» (§11.7): что вернула касса и что записано в БД. */
export interface DeviceCheckResponse {
  store_id: number
  checked_at: string
  agent_ok: boolean
  message: string
  checked: number
  updated: number
  ignored: number
  results: DeviceCheckResult[]
}

/** Тело `POST /stores/{id}/devices` — «Добавить устройство» (§11.7). */
export interface DeviceCreatePayload {
  name: string
  ip_address: string
  device_type?: string | null
  check_type?: DeviceCheckType
  check_port?: number | null
  comment?: string | null
}

/**
 * Тело `PUT /stores/{id}/devices/{device_id}`: передаются только изменяемые поля;
 * `clear_check_port` снимает TCP-порт (например, при возврате к `icmp`).
 */
export interface DeviceUpdatePayload {
  name?: string
  ip_address?: string
  device_type?: string | null
  check_type?: DeviceCheckType
  check_port?: number | null
  clear_check_port?: boolean
  is_active?: boolean
  comment?: string | null
}

/** Действия сервера над магазином (§5.2.2, §11.4). */
export type ActionName =
  | 'check_files'
  | 'download_file'
  | 'run_merge'
  | 'run_update_db'
  | 'send_message'
  | 'notify'

export interface ActionPayload {
  action: ActionName
  params?: Record<string, unknown>
}

/** Ответ одиночного действия: задача + предупреждение Q53. */
export interface ActionResponse {
  job: JobItem
  message: string
  warning: string | null
}

export interface BulkActionPayload extends ActionPayload {
  store_ids?: number[]
  cluster?: string | null
  all?: boolean
}

/** Строка прогресса массового действия (§11.4). */
export interface BulkActionJob {
  store_id: number
  store_name: string
  job_id: number | null
  status: string
  detail: string | null
}

export interface BulkActionResponse {
  action: string
  message: string
  total: number
  jobs: BulkActionJob[]
  warning: string | null
}

// --- Уведомления (§4.6–4.8, §11.5, Э5) ---------------------------------------

/** Уровни важности уведомления (§4.6): `info`/`important`/`critical`. */
export type NotificationSeverity = 'info' | 'important' | 'critical'

/** Адресация уведомления: все магазины / список / кластер (§4.6). */
export type NotificationTargetType = 'all' | 'stores' | 'cluster'

/** Статус рассылки в списке `/notifications` (§11.5). */
export type NotificationState = 'active' | 'done' | 'canceled'

/** Статус ознакомления по сессии Windows (§4.8, П-2). */
export type ReceiptStatus = 'pending' | 'shown' | 'acknowledged' | 'snoozed' | 'expired'

/** Прогресс «ознакомлены X из Y» — считается сервером по факту (§4.8). */
export interface NotificationTotals {
  stores: number
  stores_delivered: number
  sessions: number
  receipts: number
  acknowledged: number
  shown: number
  snoozed: number
  expired: number
  pending: number
  percent: number
}

export interface NotificationItem {
  id: number
  title: string
  body: string
  severity: NotificationSeverity
  requires_ack: boolean
  target_type: NotificationTargetType
  target_value: string | null
  deadline_at: string | null
  repeat_every_min: number | null
  use_msg_exe: boolean
  is_active: boolean
  state: NotificationState
  created_by: string | null
  created_at: string | null
  store_names: string[]
  totals: NotificationTotals
}

export interface NotificationList {
  items: NotificationItem[]
  total: number
  /** Счётчики по статусам: в работе / завершено / отменено (§11.5). */
  counts: Record<string, number>
  /** Плитка главной «Ждут ознакомления» (§11.1). */
  pending_receipts: number
  /** Интервал возврата отложенного — `notification_snooze_min` (§4.9, П-2). */
  snooze_min: number
}

export interface NotificationFilters {
  status?: NotificationState | 'all'
  severity?: NotificationSeverity | 'all'
  search?: string
  storeId?: number | null
  limit?: number
}

export interface NotificationCreatePayload {
  title: string
  body: string
  severity: NotificationSeverity
  requires_ack: boolean
  target_type: NotificationTargetType
  store_ids?: number[]
  cluster?: string | null
  deadline_at?: string | null
  repeat_every_min?: number | null
  use_msg_exe: boolean
}

export interface NotificationCreateResponse {
  notification: NotificationItem
  stores: number
  jobs: number
  /** Магазины, которым уведомление не ушло: отключены (§11.5). */
  skipped_store_ids: number[]
  message: string
}

/** Строка сессии в отчёте по уведомлению (§11.5). */
export interface NotificationReceiptRow {
  session_user: string
  session_id: string
  status: ReceiptStatus
  shown_at: string | null
  acknowledged_at: string | null
  ack_comment: string | null
  snooze_count: number
  snooze_until: string | null
  is_acknowledged: boolean
  is_overdue: boolean
}

export interface NotificationStoreReport {
  store_id: number
  store_name: string | null
  cluster_name: string | null
  store_is_active: boolean
  delivered_at: string | null
  sessions_count: number
  total: number
  acknowledged: number
  receipts: NotificationReceiptRow[]
}

export interface NotificationReport {
  notification: NotificationItem
  state: NotificationState
  deadline_at: string | null
  created_at: string | null
  totals: NotificationTotals
  snooze_min: number
  stores: NotificationStoreReport[]
}

export interface CancelNotificationResponse {
  notification_id: number
  state: NotificationState
  canceled_jobs: number
  message: string
}

