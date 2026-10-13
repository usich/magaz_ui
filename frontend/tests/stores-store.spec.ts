/**
 * Тесты стора магазинов (Э2): фильтры, CRUD, токен и «Проверить сейчас».
 *
 * Сеть подменяется `fetch`-моком, реальный сервер дашборда не требуется (§13).
 */

import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useStoresStore } from '@/stores/stores'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function makeStore(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 1,
    name: 'Магазин №5',
    ip_address: '10.10.5.10',
    agent_port: 8383,
    cluster_name: 'srv-1c',
    database_name: 'trade',
    version_1c: '8.3.24.1548',
    user_name_1c: 'Администратор',
    password_set: false,
    credentials_version: 1,
    devices_version: 1,
    agent_version: '1.0.0',
    os_info: 'Windows 10 Pro',
    last_seen_at: '2026-10-13T10:00:00Z',
    is_online: true,
    is_active: true,
    comment: null,
    created_at: '2026-10-13T09:00:00Z',
    updated_at: '2026-10-13T09:00:00Z',
    ...overrides,
  }
}

const HEALTH = {
  store_id: 1,
  status: 'online',
  checked_at: '2026-10-13T10:05:00Z',
  latency_ms: 12,
  agent_version: '1.0.0',
  uptime_sec: 600,
  os: 'Windows 10 Pro',
  work_dir: 'C:\\MagazAgent',
  base_1c_path: 'C:/Program Files (x86)/1cv8',
  busy: { merge: false, update: false },
  sessions: [{ user: 'user1', sid: '1' }],
  credentials_version: 1,
  devices_version: 1,
  credentials_push_required: false,
  devices_push_required: false,
}

describe('стор магазинов', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    setActivePinia(createPinia())
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('загружает список и считает онлайн/офлайн', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse([makeStore(), makeStore({ id: 2, is_online: false })]),
    )
    const stores = useStoresStore()

    await stores.load()

    expect(stores.items).toHaveLength(2)
    expect(stores.onlineCount).toBe(1)
    expect(stores.offlineCount).toBe(1)
    expect(stores.lastLoadedAt).not.toBeNull()
  })

  it('передаёт фильтры в запрос списка', async () => {
    fetchMock.mockImplementation(async () => jsonResponse([]))
    const stores = useStoresStore()
    stores.search = ' магазин '
    stores.cluster = 'srv-2'
    stores.status = 'offline'
    stores.includeInactive = true

    await stores.load()

    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toContain('/admin/stores?')
    expect(url).toContain('search=%D0%BC%D0%B0%D0%B3%D0%B0%D0%B7%D0%B8%D0%BD')
    expect(url).toContain('cluster=srv-2')
    expect(url).toContain('status=offline')
    expect(url).toContain('include_inactive=true')
  })

  it('на ошибку загрузки пишет понятный текст, не ломая список', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse({ detail: 'Требуется вход в дашборд' }, 401),
    )
    const stores = useStoresStore()

    await stores.load()

    expect(stores.errorMessage).toBe('Требуется вход в дашборд')
  })

  it('создание отправляет тело и возвращает блок конфига агента', async () => {
    const created = { ...makeStore(), agent_config: { store_id: 1, agent_token: 'tok-1' } }
    // POST — создание, GET — последующая перечитка списка (одна и та же точка)
    fetchMock.mockImplementation(async (_url: string, init: RequestInit) =>
      init?.method === 'POST' ? jsonResponse(created) : jsonResponse([makeStore()]),
    )
    const stores = useStoresStore()

    const result = await stores.create({
      name: 'Магазин №5',
      ip_address: '10.10.5.10',
      cluster_name: 'srv-1c',
      database_name: 'trade',
      version_1c: '8.3.24.1548',
      user_name_1c: 'Администратор',
      agent_port: 8383,
      password: 'Секрет1С',
    })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/admin/stores')
    expect(init.method).toBe('POST')
    expect(String(init.body)).toContain('Секрет1С')
    expect(result.agent_config.agent_token).toBe('tok-1')
    expect(stores.items).toHaveLength(1)
  })

  it('правка обновляет строку в списке', async () => {
    fetchMock.mockImplementation(async () => jsonResponse(makeStore({ name: 'Новое имя' })))
    const stores = useStoresStore()
    stores.items = [makeStore() as never]

    await stores.update(1, { name: 'Новое имя' })

    expect(stores.items[0].name).toBe('Новое имя')
  })

  it('отключение убирает магазин из активного списка', async () => {
    fetchMock.mockImplementation(async () => jsonResponse(makeStore({ is_active: false })))
    const stores = useStoresStore()
    stores.items = [makeStore() as never]

    await stores.remove(1)

    expect(stores.items).toHaveLength(0)
  })

  it('«Проверить сейчас» возвращает живой ответ агента и обновляет список', async () => {
    fetchMock.mockImplementation(async (url: string) =>
      url.endsWith('/check') ? jsonResponse(HEALTH) : jsonResponse([makeStore()]),
    )
    const stores = useStoresStore()

    const health = await stores.checkHealth(1)

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/admin/stores/1/check')
    expect(health.busy.merge).toBe(false)
    expect(health.sessions[0].user).toBe('user1')
    expect(stores.items).toHaveLength(1)
  })

  it('перевыпуск токена возвращает предупреждение', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse({ store_id: 1, agent_token: 'new-token', warning: 'потребуется перенастроить' }),
    )
    const stores = useStoresStore()

    const result = await stores.rotateToken(1)

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/admin/stores/1/token/rotate')
    expect(result.agent_token).toBe('new-token')
    expect(result.warning).toContain('перенастроить')
  })

  it('сохранение учётных данных отправляет логин и пароль (П-1)', async () => {
    fetchMock.mockImplementation(async () => jsonResponse(makeStore({ password_set: true })))
    const stores = useStoresStore()
    stores.items = [makeStore() as never]

    await stores.setCredentials(1, { user_name_1c: 'ОбменДанными', password: 'Секрет1С' })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/admin/stores/1/credentials')
    expect(init.method).toBe('PUT')
    expect(String(init.body)).toContain('ОбменДанными')
    expect(stores.items[0].password_set).toBe(true)
  })

  it('очистка пароля не отправляет значение', async () => {
    fetchMock.mockImplementation(async () => jsonResponse(makeStore({ password_set: false })))
    const stores = useStoresStore()

    await stores.setCredentials(1, { clear_password: true })

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(String(init.body)).toContain('clear_password')
  })
})
