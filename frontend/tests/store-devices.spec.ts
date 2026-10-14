/**
 * Тесты панели «Оборудование подсети» в карточке магазина (§11.2, §11.7, Э4Б).
 *
 * Проверяем сценарии админа: список со статусами, «Проверить сейчас» по
 * магазину, мягкое отключение и добавление устройства через форму. Сеть —
 * `fetch`-мок, PrimeVue-компоненты — заглушки.
 */

import { createPinia, type Pinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }))

import StoreDevicesPanel from '@/components/StoreDevicesPanel.vue'

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

const CHECK_RESULT = {
  store_id: 5,
  checked_at: '2026-10-13T10:05:00Z',
  agent_ok: true,
  message: 'Проверено устройств: 2, онлайн: 1',
  checked: 2,
  updated: 2,
  ignored: 0,
  results: [],
}

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

async function mountPanel(fetchMock: ReturnType<typeof vi.fn>, storeId = 5): Promise<VueWrapper> {
  vi.stubGlobal('fetch', fetchMock)
  const pinia: Pinia = createPinia()
  const wrapper = mount(StoreDevicesPanel, {
    props: { storeId },
    global: { plugins: [pinia], stubs },
  })
  await flushPromises()
  return wrapper
}

describe('панель оборудования в карточке магазина', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('показывает устройства магазина и счётчик «онлайн X из Y»', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse([
        makeDevice(),
        makeDevice({ id: 2, name: 'камера склад', is_active: false, is_online: false }),
      ]),
    )

    const wrapper = await mountPanel(fetchMock)

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/admin/stores/5/devices?include_inactive=true')
    expect(wrapper.findAll('[data-test^="store-device-"]')).toHaveLength(2)
    expect(wrapper.find('[data-test="store-devices-count"]').text()).toContain('онлайн 1 из 1')
    expect(wrapper.text()).toContain('камера склад')
    expect(wrapper.text()).toContain('отключено из опроса')
    wrapper.unmount()
  })

  it('«Проверить сейчас» опрашивает весь магазин и показывает итог', async () => {
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) =>
      init?.method === 'POST' ? jsonResponse(CHECK_RESULT) : jsonResponse([makeDevice()]),
    )

    const wrapper = await mountPanel(fetchMock)
    await wrapper.find('[data-test="store-devices-check"]').trigger('click')
    await flushPromises()

    const postCall = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit)?.method === 'POST',
    )
    expect(String(postCall?.[0])).toBe('/api/v1/admin/stores/5/devices/check')
    expect(String((postCall?.[1] as RequestInit).body)).toContain('"device_ids":[]')
    expect(wrapper.find('[data-test="store-devices-check-result"]').text()).toContain('онлайн: 1')
    wrapper.unmount()
  })

  it('«Отключить» делает мягкое удаление и перечитывает список', async () => {
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) =>
      init?.method === 'DELETE'
        ? jsonResponse(makeDevice({ is_active: false }))
        : jsonResponse([makeDevice()]),
    )

    const wrapper = await mountPanel(fetchMock)
    const deactivate = wrapper.findAll('button').find((item) => item.text() === 'Отключить')
    await deactivate?.trigger('click')
    await flushPromises()

    const deleteCall = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit)?.method === 'DELETE',
    )
    expect(String(deleteCall?.[0])).toBe('/api/v1/admin/stores/5/devices/1')
    expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(3)
    wrapper.unmount()
  })

  it('«Добавить устройство» сохраняет адрес подсети магазина', async () => {
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) =>
      init?.method === 'POST'
        ? jsonResponse(makeDevice({ id: 9, name: 'принтер касса', ip_address: '10.10.5.31' }))
        : jsonResponse([makeDevice()]),
    )

    const wrapper = await mountPanel(fetchMock)
    await wrapper.find('[data-test="store-devices-add"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="dialog"]').exists()).toBe(true)
    await wrapper.find('[data-test="device-form-name"]').setValue('принтер касса')
    await wrapper.find('[data-test="device-form-ip"]').setValue('10.10.5.31')
    await wrapper.find('[data-test="device-form-save"]').trigger('click')
    await flushPromises()

    const postCall = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit)?.method === 'POST',
    )
    expect(String(postCall?.[0])).toBe('/api/v1/admin/stores/5/devices')
    expect(String((postCall?.[1] as RequestInit).body)).toContain('10.10.5.31')
    wrapper.unmount()
  })

  it('пустой список объясняет, что подсеть опрашивает касса', async () => {
    fetchMock.mockImplementation(async () => jsonResponse([]))

    const wrapper = await mountPanel(fetchMock)

    expect(wrapper.find('[data-test="store-devices-empty"]').text()).toContain('касса')
    wrapper.unmount()
  })
})
