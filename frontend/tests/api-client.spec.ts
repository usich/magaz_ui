import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { API_BASE, ApiError, api, registerUnauthorizedHandler } from '@/api/client'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('api client', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('отправляет cookie сессии и возвращает разобранный JSON', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ status: 'online', app_version: '1.0.0' }))

    const result = await api.get<{ status: string; app_version: string }>('/health')

    expect(result.status).toBe('online')
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(`${API_BASE}/health`)
    expect(init.credentials).toBe('include')
    expect(init.method).toBe('GET')
  })

  it('передаёт тело и метод для POST/PUT', async () => {
    // Новый Response на каждый вызов: тело ответа можно прочитать только один раз
    fetchMock.mockImplementation(async () => jsonResponse({ ok: true }))

    await api.post('/admin/auth/login', { username: 'admin', password: 'secret' })
    await api.put('/admin/settings', { values: { health_interval_sec: '30' } })

    const [, postInit] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(postInit.method).toBe('POST')
    expect(postInit.body).toBe(JSON.stringify({ username: 'admin', password: 'secret' }))
    expect((postInit.headers as Record<string, string>)['Content-Type']).toBe('application/json')

    const [, putInit] = fetchMock.mock.calls[1] as [string, RequestInit]
    expect(putInit.method).toBe('PUT')
  })

  it('на 204 не пытается разобрать тело', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }))

    await expect(api.delete('/admin/stores/1')).resolves.toBeUndefined()
  })

  it('на 401 уведомляет обработчик и бросает ApiError с текстом сервера', async () => {
    const onUnauthorized = vi.fn()
    registerUnauthorizedHandler(onUnauthorized)
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'Требуется вход в дашборд' }, 401))

    const error = await api.get('/admin/settings').catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(401)
    expect((error as ApiError).isUnauthorized).toBe(true)
    expect((error as ApiError).message).toBe('Требуется вход в дашборд')
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('skipUnauthorizedHandler не дёргает обработчик (проверка сессии при старте)', async () => {
    const onUnauthorized = vi.fn()
    registerUnauthorizedHandler(onUnauthorized)
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'Требуется вход в дашборд' }, 401))

    await expect(api.get('/admin/auth/me', { skipUnauthorizedHandler: true })).rejects.toThrow()

    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('разбирает 422 от FastAPI в список сообщений', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        { detail: [{ loc: ['body', 'values'], msg: 'Настройка должна быть целым числом', type: 'value_error' }] },
        422,
      ),
    )

    const error = (await api.put('/admin/settings', { values: {} }).catch((caught) => caught)) as ApiError

    expect(error.isValidation).toBe(true)
    expect(error.messages).toEqual(['Настройка должна быть целым числом'])
    expect(error.message).toBe('Настройка должна быть целым числом')
  })

  it('409 помечается как конфликт операции', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'На агенте уже выполняется операция' }, 409))

    const error = (await api.post('/admin/stores/1/actions').catch((caught) => caught)) as ApiError

    expect(error.isConflict).toBe(true)
    expect(error.message).toBe('На агенте уже выполняется операция')
  })

  it('сетевой сбой превращается в ApiError со статусом 0', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))

    const error = (await api.get('/admin/settings').catch((caught) => caught)) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(0)
    expect(error.message).toBe('Сервер дашборда недоступен')
  })
})
