/**
 * Тесты страницы «Оборудование подсети» (§11.7, Э4Б).
 *
 * PrimeVue-компоненты подменяются лёгкими заглушками: проверяем логику страницы
 * (сводная таблица, фильтры, «Проверить сейчас», подсказка на пустом списке),
 * а не оформление. Сеть — `fetch`-мок, маршрут — память вместо истории.
 */

import { createPinia, type Pinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }))

import DevicesView from '@/views/DevicesView.vue'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function makeDevice(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 1,
    store_id: 5,
    store_name: 'Магазин №5',
    cluster_name: 'srv-1c',
    store_is_active: true,
    store_is_online: true,
    name: 'камера вход',
    ip_address: '10.10.5.21',
    device_type: 'camera',
    check_type: 'icmp',
    check_port: null,
    is_active: true,
    last_status: 'online',
    last_seen_at: '2026-10-13T10:00:00Z',
    last_latency_ms: 3,
    last_error: null,
    comment: null,
    is_online: true,
    ...overrides,
  }
}

function makeMatrix(rows: Record<string, unknown>[]): Record<string, unknown> {
  return {
    rows,
    total: rows.length,
    online: rows.filter((row) => row.is_online).length,
    offline: rows.filter((row) => row.last_status === 'offline').length,
    unknown: rows.filter((row) => row.last_status === null).length,
    stores: new Set(rows.map((row) => row.store_id)).size,
    window_sec: 180,
  }
}

const CHECK_RESULT = {
  store_id: 5,
  checked_at: '2026-10-13T10:05:00Z',
  agent_ok: true,
  message: 'Проверено устройств: 1, онлайн: 0',
  checked: 1,
  updated: 1,
  ignored: 0,
  results: [{ ip: '10.10.5.21', status: 'offline', latency_ms: null, error: 'нет ответа' }],
}

/**
 * Мок админского API: матрица устройств, список магазинов (нужен фильтру и форме)
 * и ответ «Проверить сейчас». Порядок проверок важен: `/admin/stores` — часть
 * адресов устройств, поэтому ветка проверки идёт первой.
 */
function apiMock(
  matrix: unknown,
  stores: unknown[] = [{ id: 5, name: 'Магазин №5' }],
): (url: string, init?: RequestInit) => Promise<Response> {
  return async (url: string, init?: RequestInit) => {
    if (url.includes('/devices/check')) return jsonResponse(CHECK_RESULT)
    if (url.includes('/admin/devices/matrix')) return jsonResponse(matrix)
    // Правки карточки устройства: ответ значения не имеет — важно, что запрос ушёл
    if (init?.method && init.method !== 'GET') return jsonResponse(matrix)
    return jsonResponse(stores)
  }
}

/** Заглушки PrimeVue: сохраняют контракт v-model и пробрасывают `data-test` из `pt`. */
const stubs = {
  InputText: {
    props: ['modelValue', 'pt'],
    emits: ['update:modelValue'],
    template: `<input
      :data-test="pt && pt.root ? pt.root['data-test'] : undefined"
      :value="modelValue"
      @input="$emit('update:modelValue', $event.target.value)"
    />`,
  },
  Button: {
    props: ['label', 'disabled', 'pt'],
    emits: ['click'],
    template: `<button
      :data-test="pt && pt.root ? pt.root['data-test'] : undefined"
      :disabled="disabled"
      @click="$emit('click')"
    >{{ label }}</button>`,
  },
  Tag: { props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  Message: { template: '<div class="stub-message"><slot /></div>' },
  Dialog: {
    props: ['visible', 'header'],
    template: `<section v-if="visible" data-test="dialog"><h3>{{ header }}</h3><slot /><slot name="footer" /></section>`,
  },
}

async function mountDevices(
  fetchMock: ReturnType<typeof vi.fn>,
  path = '/devices',
): Promise<{ wrapper: VueWrapper; router: Router; pinia: Pinia }> {
  vi.stubGlobal('fetch', fetchMock)
  const pinia: Pinia = createPinia()
  const router: Router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/devices', name: 'devices', component: DevicesView },
      { path: '/stores/:id(\\d+)', name: 'store-detail', component: { template: '<div />' } },
    ],
  })
  await router.push(path)
  await router.isReady()

  const wrapper = mount(DevicesView, { global: { plugins: [pinia, router], stubs } })
  await flushPromises()
  return { wrapper, router, pinia }
}

describe('страница «Оборудование подсети»', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('показывает строки сводной таблицы со статусом и способом проверки', async () => {
    fetchMock.mockImplementation(
      apiMock(
        makeMatrix([
          makeDevice({ id: 1, name: 'камера вход' }),
          makeDevice({
            id: 2,
            name: 'камера склад',
            ip_address: '10.10.5.22',
            device_type: 'camera',
            check_type: 'tcp',
            check_port: 554,
            last_status: 'offline',
            is_online: false,
            last_latency_ms: null,
          }),
        ]),
      ),
    )

    const { wrapper } = await mountDevices(fetchMock)

    const rows = wrapper.findAll('[data-test^="device-row-"]')
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('камера вход')
    expect(rows[0].text()).toContain('Магазин №5')
    expect(rows[0].text()).toContain('ping (ICMP)')
    expect(rows[1].text()).toContain('TCP-порт:554')
    expect(rows[1].text()).toContain('офлайн')
    expect(wrapper.find('[data-test="devices-summary"]').text()).toContain('Онлайн 1 из 2')
    wrapper.unmount()
  })

  it('плитка главной открывает страницу с фильтром «офлайн» (§11.1)', async () => {
    fetchMock.mockImplementation(apiMock(makeMatrix([])))

    const { wrapper } = await mountDevices(fetchMock, '/devices?status=offline')

    const matrixCall = fetchMock.mock.calls.find((call) =>
      String(call[0]).includes('/admin/devices/matrix'),
    )
    expect(String(matrixCall?.[0])).toContain('status=offline')
    wrapper.unmount()
  })

  it('снятая галочка «показывать отключённые» уходит на сервер', async () => {
    fetchMock.mockImplementation(apiMock(makeMatrix([])))

    const { wrapper } = await mountDevices(fetchMock)
    const checkbox = wrapper.find('[data-test="devices-include-inactive"]')
    await checkbox.setValue(false)
    await flushPromises()

    const urls = fetchMock.mock.calls.map((call) => String(call[0]))
    expect(urls.some((url) => url.includes('include_inactive=false'))).toBe(true)
    wrapper.unmount()
  })

  it('«Проверить» по строке просит кассу проверить именно этот адрес', async () => {
    fetchMock.mockImplementation(
      apiMock(
        makeMatrix([makeDevice({ id: 4, store_id: 5, last_status: 'offline', is_online: false })]),
      ),
    )

    const { wrapper } = await mountDevices(fetchMock)
    const checkButton = wrapper
      .findAll('button')
      .find((item) => item.text() === 'Проверить')
    expect(checkButton).toBeTruthy()
    await checkButton?.trigger('click')
    await flushPromises()

    const checkCall = fetchMock.mock.calls.find((call) =>
      String(call[0]).includes('/devices/check'),
    )
    expect(String(checkCall?.[0])).toBe('/api/v1/admin/stores/5/devices/check')
    expect(String((checkCall?.[1] as RequestInit).body)).toContain('[4]')
    expect(wrapper.find('[data-test="devices-check-result"]').text()).toContain('онлайн: 0')
    wrapper.unmount()
  })

  it('отключённый магазин и устройство вне опроса не проверяются по кнопке', async () => {
    fetchMock.mockImplementation(
      apiMock(
        makeMatrix([
          makeDevice({ id: 6, store_is_active: false, store_is_online: false }),
          makeDevice({ id: 7, ip_address: '10.10.5.24', is_active: false, is_online: false }),
        ]),
      ),
    )

    const { wrapper } = await mountDevices(fetchMock)

    for (const id of [6, 7]) {
      const row = wrapper.find(`[data-test="device-row-${id}"]`)
      const button = row.findAll('button').find((item) => item.text() === 'Проверить')
      expect(button?.attributes('disabled'), `строка ${id}: проверка недоступна`).toBeDefined()
    }
    wrapper.unmount()
  })

  it('пустой список объясняет, откуда добавлять устройства', async () => {
    fetchMock.mockImplementation(apiMock(makeMatrix([])))

    const { wrapper } = await mountDevices(fetchMock)

    expect(wrapper.find('[data-test="devices-empty"]').text()).toContain('карточку магазина')
    wrapper.unmount()
  })
})
