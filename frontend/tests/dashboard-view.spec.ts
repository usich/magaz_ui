/**
 * Тесты главной страницы (§11.1).
 *
 * Отдельно закреплён Д-14: плитка магазинов показывает число магазинов **в
 * работе**, а не число строк в таблице. «Отключить» на странице магазинов —
 * мягкое удаление (§5.2.2), поэтому строка остаётся в БД, и раньше главная
 * продолжала считать отключённый магазин рабочим.
 *
 * PrimeVue-компоненты подменяются лёгкими заглушками: проверяем числа и текст
 * подсказок, а не оформление. Сеть — `fetch`-мок, маршрут — память.
 */

import { createPinia, type Pinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DashboardView from '@/views/DashboardView.vue'
import { useSystemStore } from '@/stores/system'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** Счётчики `/admin/system/info`: по умолчанию 2 строки в БД и 1 магазин в работе. */
function makeCounts(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    stores_total: 2,
    stores_active: 1,
    stores_inactive: 1,
    stores_online: 0,
    stores_offline: 1,
    online_window_sec: 45,
    admins_total: 1,
    audit_events: 0,
    jobs_active: 0,
    devices_total: 0,
    devices_online: 0,
    devices_window_sec: 180,
    notifications_pending: 0,
    notifications_snooze_min: 15,
    ...overrides,
  }
}

function makeSystemInfo(counts: Record<string, unknown>): Record<string, unknown> {
  return {
    app_name: 'Magaz UI',
    app_version: '1.0.0',
    database: { kind: 'sqlite', schema_version: 'c4f7a1b95e20' },
    spa: { dist_dir: 'frontend/dist', built: true },
    counts,
    settings_keys: 14,
  }
}

/** Карточка магазина в списке `/admin/stores` — колонки §4.1. */
function makeStore(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 1,
    name: 'Магазин №1',
    ip_address: '10.10.1.1',
    agent_port: 8383,
    cluster_name: 'srv-1c',
    database_name: 'trade',
    version_1c: '8.3.24.1548',
    user_name_1c: 'Администратор',
    password_set: true,
    credentials_version: 1,
    devices_version: 1,
    agent_version: '1.0.0',
    os_info: 'Windows 10',
    last_seen_at: '2026-10-14T09:00:00Z',
    is_online: false,
    is_active: true,
    comment: null,
    created_at: '2026-10-13T09:00:00Z',
    updated_at: '2026-10-13T09:00:00Z',
    ...overrides,
  }
}

/** Мок API: счётчики, список магазинов и бэкапы (Д-21). */
function apiMock(
  counts: Record<string, unknown>,
  stores: unknown[],
  backupStatus: Record<string, unknown> = {
    stores: { total: 0, checked: 0, ok: 0, missing: 0 },
    local_bases: { total: 0, checked: 0, ok: 0, missing: 0 },
  },
  backupStores: unknown[] = [],
  backupBases: unknown[] = [],
): (url: string) => Promise<Response> {
  return async (url: string) => {
    if (url.includes('/admin/system/info')) return jsonResponse(makeSystemInfo(counts))
    if (url.includes('/admin/backup/status')) return jsonResponse(backupStatus)
    if (url.includes('/admin/backup/stores')) return jsonResponse(backupStores)
    if (url.includes('/admin/backup/local-bases')) return jsonResponse(backupBases)
    return jsonResponse(stores)
  }
}

/** Заглушки PrimeVue: контракт виден по тексту, оформление не проверяем. */
const stubs = {
  Button: { props: ['label'], template: '<button>{{ label }}</button>' },
  Tag: { props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  Message: { template: '<div class="stub-message"><slot /></div>' },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /></div>' },
}

async function mountDashboard(fetchMock: ReturnType<typeof vi.fn>): Promise<VueWrapper> {
  vi.stubGlobal('fetch', fetchMock)
  const pinia: Pinia = createPinia()
  const router: Router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'dashboard', component: DashboardView },
      { path: '/stores', name: 'stores', component: { template: '<div />' } },
      { path: '/stores/:id(\\d+)', name: 'store-detail', component: { template: '<div />' } },
      { path: '/devices', name: 'devices', component: { template: '<div />' } },
      { path: '/notifications', name: 'notifications', component: { template: '<div />' } },
    ],
  })
  await router.push('/')
  await router.isReady()

  const wrapper = mount(DashboardView, { global: { plugins: [pinia, router], stubs } })
  // Счётчики диагностики грузит оболочка дашборда `AppShell` (§11.1); в тесте
  // страницы её нет — запрашиваем данные тем же стором, что и оболочка.
  await useSystemStore(pinia).load()
  await flushPromises()
  return wrapper
}

describe('главная страница', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('плитка магазинов считает магазины в работе, а не строки таблицы (Д-14)', async () => {
    fetchMock.mockImplementation(apiMock(makeCounts(), [makeStore()]))

    const wrapper = await mountDashboard(fetchMock)
    const tile = wrapper.find('[data-test="tile-stores"]')

    expect(tile.text()).toContain('Магазинов в работе')
    expect(tile.text()).toContain('активных · отключено 1')
    expect(tile.find('[data-test="tile-value"]').text()).toBe('1')
    expect(wrapper.findAll('[data-test="dashboard-store-row"]')).toHaveLength(1)
    wrapper.unmount()
  })

  it('без отключённых магазинов подсказка плитки простая', async () => {
    fetchMock.mockImplementation(
      apiMock(makeCounts({ stores_total: 1, stores_inactive: 0, stores_active: 1 }), [makeStore()]),
    )

    const wrapper = await mountDashboard(fetchMock)

    expect(wrapper.find('[data-test="tile-stores"]').text()).toContain('активных магазинов')
    wrapper.unmount()
  })

  it('блок «Бэкапы» показывает «есть N из M» по магазинам и локальным базам (Д-21)', async () => {
    fetchMock.mockImplementation(
      apiMock(
        makeCounts(),
        [makeStore()],
        {
          stores: { total: 30, checked: 28, ok: 28, missing: 2 },
          local_bases: { total: 3, checked: 3, ok: 3, missing: 0 },
        },
        [
          { kind: 'store', id: 1, name: 'Магазин №5', backup_status: 'missing', backup_error: 'нет', backup_checked_date: '2026-10-22', backup_check_yesterday: false },
        ],
      ),
    )

    const wrapper = await mountDashboard(fetchMock)

    expect(wrapper.find('[data-test="backup-dashboard"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="backup-stores"]').text()).toContain('есть 28 из 30')
    expect(wrapper.find('[data-test="backup-local-bases"]').text()).toContain('есть 3 из 3')

    // провал в список
    await wrapper.find('[data-test="backup-stores"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="backup-list-dialog"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Магазин №5')
    wrapper.unmount()
  })

})
