import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useAuthStore } from '@/stores/auth'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const ADMIN = {
  id: 1,
  username: 'admin',
  is_active: true,
  last_login_at: '2026-10-13T10:00:00Z',
}

describe('стор аутентификации', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    setActivePinia(createPinia())
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('после успешного входа хранит профиль администратора', async () => {
    fetchMock.mockResolvedValue(jsonResponse(ADMIN))
    const auth = useAuthStore()

    await expect(auth.login('admin', 'magaz-test-password')).resolves.toBe(true)

    expect(auth.isAuthenticated).toBe(true)
    expect(auth.user?.username).toBe('admin')
    expect(auth.errorMessage).toBe('')
  })

  it('на неверный пароль даёт понятный текст и не пускает дальше', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'Неверный логин или пароль' }, 401))
    const auth = useAuthStore()

    await expect(auth.login('admin', 'wrong')).resolves.toBe(false)

    expect(auth.isAuthenticated).toBe(false)
    expect(auth.user).toBeNull()
    expect(auth.errorMessage).toBe('Неверный логин или пароль')
  })

  it('fetchCurrentUser запрашивает сервер только один раз', async () => {
    fetchMock.mockResolvedValue(jsonResponse(ADMIN))
    const auth = useAuthStore()

    await auth.fetchCurrentUser()
    await auth.fetchCurrentUser()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(auth.isAuthenticated).toBe(true)
  })

  it('fetchCurrentUser при 401 оставляет пользователя неавторизованным', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'Требуется вход в дашборд' }, 401))
    const auth = useAuthStore()

    await expect(auth.fetchCurrentUser()).resolves.toBeNull()

    expect(auth.isInitialized).toBe(true)
    expect(auth.isAuthenticated).toBe(false)
  })

  it('logout сбрасывает состояние даже при ошибке сервера', async () => {
    fetchMock.mockResolvedValue(jsonResponse(ADMIN))
    const auth = useAuthStore()
    await auth.login('admin', 'magaz-test-password')

    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    await auth.logout()

    expect(auth.user).toBeNull()
    expect(auth.errorMessage).toBe('')
  })

  it('reset сбрасывает кэш инициализации (после 401 из любого запроса)', async () => {
    fetchMock.mockResolvedValue(jsonResponse(ADMIN))
    const auth = useAuthStore()
    await auth.fetchCurrentUser()

    auth.reset()

    expect(auth.user).toBeNull()
    expect(auth.isInitialized).toBe(false)
  })
})
