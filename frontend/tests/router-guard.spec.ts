import { createPinia } from 'pinia'
import { createMemoryHistory, type Router } from 'vue-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '@/router'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const ADMIN = { id: 1, username: 'admin', is_active: true, last_login_at: null }

async function setupRouter(fetchMock: ReturnType<typeof vi.fn>): Promise<Router> {
  vi.stubGlobal('fetch', fetchMock)
  const router = createAppRouter(createPinia(), createMemoryHistory())
  return router
}

describe('guard роутера (§5.2.1)', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('без сессии уводит на страницу входа и запоминает адрес', async () => {
    const router = await setupRouter(
      vi.fn().mockResolvedValue(jsonResponse({ detail: 'Требуется вход в дашборд' }, 401)),
    )

    await router.push('/stores')
    await router.isReady()

    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/stores')
  })

  it('с действующей сессией открывает защищённый маршрут', async () => {
    const router = await setupRouter(vi.fn().mockResolvedValue(jsonResponse(ADMIN)))

    await router.push('/settings')
    await router.isReady()

    expect(router.currentRoute.value.name).toBe('settings')
  })

  it('вошедшего администратора со страницы входа уводит на главную', async () => {
    const router = await setupRouter(vi.fn().mockResolvedValue(jsonResponse(ADMIN)))

    await router.push('/login')
    await router.isReady()

    expect(router.currentRoute.value.name).toBe('dashboard')
  })

  it('на главную без параметра redirect не подставляет пустой адрес', async () => {
    const router = await setupRouter(
      vi.fn().mockResolvedValue(jsonResponse({ detail: 'Требуется вход в дашборд' }, 401)),
    )

    await router.push('/')
    await router.isReady()

    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBeUndefined()
  })

  it('неизвестный адрес отдаёт страницу 404 без проверки сессии', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: 'nope' }, 401))
    const router = await setupRouter(fetchMock)

    await router.push('/которого-нет')
    await router.isReady()

    expect(router.currentRoute.value.name).toBe('not-found')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('маршруты §5.2.1 существуют в таблице маршрутов', async () => {
    const router = await setupRouter(vi.fn().mockResolvedValue(jsonResponse(ADMIN)))
    const names = new Set(router.getRoutes().map((route) => route.name))

    for (const expected of [
      'login',
      'dashboard',
      'stores',
      'store-detail',
      'files',
      'devices',
      'notifications',
      'notification-detail',
      'jobs',
      'audit',
      'settings',
    ]) {
      expect(names.has(expected), `маршрут ${expected} отсутствует`).toBe(true)
    }
  })
})
