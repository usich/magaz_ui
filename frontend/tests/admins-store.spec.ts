/**
 * Тесты стора администраторов (Д-8, §11.6, Э6).
 *
 * Сеть подменяется `fetch`-моком: проверяется то, за что отвечает стор —
 * адреса и тела запросов, перечитывание списка после изменения, счётчик
 * действующих и текст отказа сервера (`403` «нельзя себя», `409` «занят логин»),
 * который интерфейс показывает как есть.
 */

import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ADMIN_PASSWORD_MIN_LENGTH, useAdminsStore } from '@/stores/admins'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function makeAdmin(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 1,
    username: 'admin',
    is_active: true,
    last_login_at: '2026-10-14T08:00:00Z',
    ...overrides,
  }
}

/** Список читается `GET`-ом, поэтому поиск своего запроса идёт по методу. */
function readCall(fetchMock: ReturnType<typeof vi.fn>): [string, RequestInit] {
  return fetchMock.mock.calls.find((call) => call[1]?.method === 'GET') as [string, RequestInit]
}

describe('стор администраторов', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    setActivePinia(createPinia())
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('читает список и считает действующих (§11.6)', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse([makeAdmin(), makeAdmin({ id: 2, username: 'operator', is_active: false })]),
    )
    const store = useAdminsStore()

    await store.load()

    expect(readCall(fetchMock)[0]).toBe('/api/v1/admin/admins')
    expect(store.items).toHaveLength(2)
    expect(store.activeCount).toBe(1)
    expect(store.errorMessage).toBe('')
    expect(store.lastLoadedAt).toBeInstanceOf(Date)
    expect(store.findById(2)?.username).toBe('operator')
    expect(store.findById(null)).toBeNull()
  })

  it('при ошибке чтения показывает текст сервера и не теряет список', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse([makeAdmin()]))
    const store = useAdminsStore()
    await store.load()

    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: 'Внутренняя ошибка' }, 500))
    await store.load()

    expect(store.errorMessage).toBe('Внутренняя ошибка')
    // Показанные строки не затёрты: ошибка не «обнуляет» таблицу
    expect(store.items).toHaveLength(1)
  })

  it('создаёт администратора и перечитывает список (Д-8)', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(makeAdmin({ id: 5, username: 'operator' }), 201),
    )
    fetchMock.mockResolvedValueOnce(
      jsonResponse([makeAdmin(), makeAdmin({ id: 5, username: 'operator' })]),
    )
    const store = useAdminsStore()

    const created = await store.create({ username: 'operator', password: 'operator-password-1' })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/admin/admins')
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({
      username: 'operator',
      password: 'operator-password-1',
    })
    expect(created?.id).toBe(5)
    expect(store.items).toHaveLength(2)
  })

  it('занятый логин: возвращает null и объясняет причину (409)', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: "Администратор 'admin' уже существует" }, 409),
    )
    const store = useAdminsStore()

    const created = await store.create({ username: 'admin', password: 'admin-password-1' })

    expect(created).toBeNull()
    expect(store.errorMessage).toContain('уже существует')
  })

  it('меняет пароль через PUT (Д-8)', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(makeAdmin({ id: 2, username: 'operator' })))
    fetchMock.mockResolvedValueOnce(jsonResponse([makeAdmin({ id: 2, username: 'operator' })]))
    const store = useAdminsStore()

    const updated = await store.update(2, { password: 'operator-password-new' })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/admin/admins/2')
    expect(init.method).toBe('PUT')
    expect(JSON.parse(String(init.body))).toEqual({ password: 'operator-password-new' })
    expect(updated?.username).toBe('operator')
  })

  it('блокировка идёт той же точкой, что смена пароля, — с `is_active`', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(makeAdmin({ id: 2, username: 'operator', is_active: false })),
    )
    fetchMock.mockResolvedValueOnce(
      jsonResponse([makeAdmin(), makeAdmin({ id: 2, username: 'operator', is_active: false })]),
    )
    const store = useAdminsStore()

    await store.setActive(2, false)

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({ is_active: false })
    expect(store.activeCount).toBe(1)
  })

  it('запрет на себя: возвращает null и показывает текст 403', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: 'Нельзя заблокировать или удалить свою учётную запись' }, 403),
    )
    const store = useAdminsStore()

    const updated = await store.setActive(1, false)

    expect(updated).toBeNull()
    expect(store.errorMessage).toContain('свою учётную запись')
  })

  it('удаляет администратора и перечитывает список (Д-8)', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true }))
    fetchMock.mockResolvedValueOnce(jsonResponse([makeAdmin()]))
    const store = useAdminsStore()

    const removed = await store.remove(2)

    expect(removed).toBe(true)
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/admin/admins/2')
    expect((fetchMock.mock.calls[0]?.[1] as RequestInit).method).toBe('DELETE')
    expect(store.items).toHaveLength(1)
  })

  it('отказ удаления (последний действующий) не считается успехом (409)', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: 'Это последний действующий администратор' }, 409),
    )
    const store = useAdminsStore()

    const removed = await store.remove(1)

    expect(removed).toBe(false)
    expect(store.errorMessage).toContain('последний действующий')
  })

  it('подсказка о длине пароля совпадает с минимальной длиной сервера', () => {
    expect(ADMIN_PASSWORD_MIN_LENGTH).toBe(8)
  })
})
