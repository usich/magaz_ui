/**
 * Тесты страницы «Магазины» (Э2, §11.2).
 *
 * PrimeVue-компоненты подменяются лёгкими заглушками: проверяем логику страницы
 * (список, фильтры, создание магазина и блок конфига агента), а не тему
 * оформления. Сеть — `fetch`-мок.
 */

import { createPinia, setActivePinia, type Pinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Toast-сервис живёт в приложении; в тестах достаточно заглушки
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }))

import StoresView from '@/views/StoresView.vue'

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

/** Заглушки PrimeVue: сохраняют контракт v-model и пробрасывают `data-test` из `pt`. */
const stubs = {
  RouterLink: { template: '<a><slot /></a>' },
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
  Dialog: {
    props: ['visible', 'header'],
    template: `<section v-if="visible" :data-test="'dialog'"><h3>{{ header }}</h3><slot /><slot name="footer" /></section>`,
  },
  Tag: { props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  Message: { template: '<div class="stub-message"><slot /></div>' },
}

async function mountStores(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal('fetch', fetchMock)
  const pinia: Pinia = createPinia()
  setActivePinia(pinia)

  const wrapper = mount(StoresView, { global: { plugins: [pinia], stubs } })
  await flushPromises()
  return wrapper
}

describe('страница «Магазины»', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('показывает магазины со статусами из API', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse([makeStore(), makeStore({ id: 2, name: 'Склад', is_online: false })]),
    )

    const wrapper = await mountStores(fetchMock)

    const rows = wrapper.findAll('[data-test="store-row"]')
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('Магазин №5')
    expect(rows[0].text()).toContain('10.10.5.10')
    expect(rows[1].text()).toContain('Склад')
    expect(wrapper.text()).toContain('онлайн')
    expect(wrapper.text()).toContain('офлайн')

    wrapper.unmount()
  })

  it('смена фильтра статуса перезапрашивает список с сервера', async () => {
    fetchMock.mockImplementation(async () => jsonResponse([makeStore()]))

    const wrapper = await mountStores(fetchMock)
    fetchMock.mockClear()

    await wrapper.find('[data-test="store-status-filter"]').setValue('offline')
    await flushPromises()

    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toContain('status=offline')
    wrapper.unmount()
  })

  it('создание магазина отправляет данные и показывает блок конфига агента', async () => {
    const created = {
      ...makeStore({ id: 7, name: 'Магазин №7' }),
      agent_config: {
        store_id: 7,
        server_url: 'http://10.10.0.1:8080',
        port: 8383,
        agent_token: 'fresh-token',
        config_path: 'C:\\MagazAgent\\agent-config.json',
        config_json: { store_id: 7, agent_token: 'fresh-token' },
        notes: ['Пароль 1С в конфиг не вписывается'],
      },
    }
    fetchMock.mockImplementation(async (_url: string, init: RequestInit) =>
      init?.method === 'POST' ? jsonResponse(created) : jsonResponse([makeStore()]),
    )

    const wrapper = await mountStores(fetchMock)
    await wrapper.find('[data-test="add-store"]').trigger('click')

    await wrapper.find('[data-test="form-name"]').setValue('Магазин №7')
    await wrapper.find('[data-test="form-ip"]').setValue('10.10.7.7')
    await wrapper.find('[data-test="form-cluster"]').setValue('srv-1c')
    await wrapper.find('[data-test="form-database"]').setValue('trade')
    await wrapper.find('[data-test="form-version"]').setValue('8.3.24.1548')
    await wrapper.find('[data-test="form-user"]').setValue('Администратор')
    await wrapper.find('[data-test="save-store"]').trigger('click')
    await flushPromises()

    const postCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit)?.method === 'POST')
    expect(postCall).toBeTruthy()
    expect(String((postCall?.[1] as RequestInit).body)).toContain('10.10.7.7')

    expect(wrapper.find('[data-test="agent-config-json"]').text()).toContain('fresh-token')
    expect(wrapper.text()).toContain('Что вписать в конфиг агента')
    wrapper.unmount()
  })

  it('показывает текст ошибки, если сервер отклонил создание', async () => {
    fetchMock.mockImplementation(async (_url: string, init: RequestInit) =>
      init?.method === 'POST'
        ? jsonResponse({ detail: 'Магазин с IP 10.10.7.7 уже существует' }, 422)
        : jsonResponse([makeStore()]),
    )

    const wrapper = await mountStores(fetchMock)
    await wrapper.find('[data-test="add-store"]').trigger('click')

    await wrapper.find('[data-test="form-name"]').setValue('Магазин №7')
    await wrapper.find('[data-test="form-ip"]').setValue('10.10.7.7')
    await wrapper.find('[data-test="form-cluster"]').setValue('srv-1c')
    await wrapper.find('[data-test="form-database"]').setValue('trade')
    await wrapper.find('[data-test="form-version"]').setValue('8.3.24.1548')
    await wrapper.find('[data-test="form-user"]').setValue('Администратор')
    await wrapper.find('[data-test="save-store"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('уже существует')
    wrapper.unmount()
  })

  it('показывает новые токены после перевыпуска', async () => {
    fetchMock.mockImplementation(async (url: string) =>
      url.endsWith('/token/rotate')
        ? jsonResponse({ store_id: 1, agent_token: 'rotated-token', warning: 'перенастройте агент' })
        : jsonResponse([makeStore()]),
    )

    const wrapper = await mountStores(fetchMock)
    const buttons = wrapper.findAll('button').filter((item) => item.text() === 'Токен')
    await buttons[0].trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="new-token"]').text()).toContain('rotated-token')
    expect(wrapper.text()).toContain('перенастройте агент')
    wrapper.unmount()
  })
})
