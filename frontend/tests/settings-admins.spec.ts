/**
 * Тесты блока «Администраторы» на странице `/settings` (§11.6, Д-8, Э6).
 *
 * PrimeVue-компоненты подменяются лёгкими заглушками: проверяется логика
 * страницы — список, пометка «это вы», блокировка, создание и удаление — а не
 * оформление. Сеть — `fetch`-мок, реальный сервер дашборда не нужен.
 */

import { createPinia, type Pinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }))

import SettingsView from '@/views/SettingsView.vue'
import { useAuthStore } from '@/stores/auth'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const SETTINGS = [
  {
    key: 'notification_snooze_min',
    value: '15',
    default: '15',
    is_default: true,
    description: 'Через сколько минут вернуть отложенное уведомление',
  },
]

function makeAdmin(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 1,
    username: 'admin',
    is_active: true,
    last_login_at: '2026-10-14T08:00:00Z',
    ...overrides,
  }
}

const OPERATOR = makeAdmin({ id: 2, username: 'operator', last_login_at: null })

/**
 * Мок админского API. Ветки идут от частных адресов к общим: `/admin/admins/2`
 * проверяется раньше `/admin/admins`, иначе правка «съедается» списком.
 */
function apiMock(overrides: { admins?: unknown[]; adminUpdate?: Response } = {}) {
  const admins = overrides.admins ?? [makeAdmin(), OPERATOR]
  return async (url: string, init?: RequestInit): Promise<Response> => {
    const method = init?.method ?? 'GET'
    if (url.includes('/admin/admins/')) {
      if (method === 'DELETE') return jsonResponse({ ok: true })
      if (overrides.adminUpdate) return overrides.adminUpdate
      const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {}
      return jsonResponse(
        makeAdmin({ id: 2, username: 'operator', is_active: body.is_active ?? true }),
      )
    }
    if (url.includes('/admin/admins')) {
      if (method === 'POST') {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>
        return jsonResponse(makeAdmin({ id: 3, username: body.username }), 201)
      }
      return jsonResponse(admins)
    }
    return jsonResponse(SETTINGS)
  }
}

/** Заглушки PrimeVue: сохраняют контракт v-model и пробрасывают `data-test`. */
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
    props: ['label', 'disabled'],
    emits: ['click'],
    template: `<button :disabled="disabled" @click="$emit('click')">{{ label }}</button>`,
  },
  Tag: { props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  Message: { template: '<div class="stub-message"><slot /></div>' },
  Dialog: {
    props: ['visible', 'header'],
    template: `<section v-if="visible" data-test="dialog"><h3>{{ header }}</h3><slot /><slot name="footer" /></section>`,
  },
}

async function mountSettings(
  fetchMock: ReturnType<typeof vi.fn>,
): Promise<{ wrapper: VueWrapper; pinia: Pinia }> {
  vi.stubGlobal('fetch', fetchMock)
  const pinia: Pinia = createPinia()
  const auth = useAuthStore(pinia)
  auth.user = { id: 1, username: 'admin', is_active: true, last_login_at: null }
  const wrapper = mount(SettingsView, { global: { plugins: [pinia], stubs } })
  await flushPromises()
  return { wrapper, pinia }
}

/** Тело запроса как объект — для проверки контракта `PUT`/`POST`. */
function bodyOf(call: [string, RequestInit] | undefined): Record<string, unknown> {
  return call?.[1]?.body ? (JSON.parse(String(call[1].body)) as Record<string, unknown>) : {}
}

describe('блок «Администраторы» на странице настроек', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('показывает список, счётчик действующих и пометку «это вы» (§11.6)', async () => {
    fetchMock.mockImplementation(apiMock())

    const { wrapper } = await mountSettings(fetchMock)

    expect(fetchMock.mock.calls.some((call) => call[0] === '/api/v1/admin/admins')).toBe(true)
    expect(wrapper.find('[data-test="admins-active-count"]').text()).toBe('2')
    expect(wrapper.find('[data-test="admins-total-count"]').text()).toBe('2')
    expect(wrapper.find('[data-test="admin-row-2"]').text()).toContain('operator')
    expect(wrapper.find('[data-test="admin-last-login-2"]').text()).toBe('не входил')
    expect(wrapper.find('[data-test="admin-status-2"]').text()).toBe('действует')
    expect(wrapper.find('[data-test="admin-row-1"]').text()).toContain('это вы')
    // Свою учётную запись нельзя удалить — кнопки в строке нет
    expect(wrapper.find('[data-test="admin-delete-1"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="admin-delete-2"]').exists()).toBe(true)
  })

  it('строка заблокированного показывает статус и предлагает разблокировку', async () => {
    fetchMock.mockImplementation(
      apiMock({ admins: [makeAdmin(), makeAdmin({ id: 2, username: 'operator', is_active: false })] }),
    )

    const { wrapper } = await mountSettings(fetchMock)

    expect(wrapper.find('[data-test="admin-status-2"]').text()).toBe('заблокирован')
    expect(wrapper.find('[data-test="admin-toggle-2"]').text()).toBe('Разблокировать')
    expect(wrapper.find('[data-test="admins-active-count"]').text()).toBe('1')
  })

  it('«Заблокировать» отправляет PUT с `is_active=false` и перечитывает список', async () => {
    fetchMock.mockImplementation(apiMock())

    const { wrapper } = await mountSettings(fetchMock)
    await wrapper.find('[data-test="admin-toggle-2"]').trigger('click')
    await flushPromises()

    const putCall = fetchMock.mock.calls.find(
      (call) => String(call[0]) === '/api/v1/admin/admins/2' && call[1]?.method === 'PUT',
    ) as [string, RequestInit]
    expect(bodyOf(putCall)).toEqual({ is_active: false })
    expect(fetchMock.mock.calls.at(-1)?.[0]).toBe('/api/v1/admin/admins')
  })

  it('отказ сервера на блокировку показывается в блоке (§11.6)', async () => {
    fetchMock.mockImplementation(
      apiMock({
        adminUpdate: jsonResponse(
          { detail: 'Нельзя заблокировать или удалить свою учётную запись' },
          403,
        ),
      }),
    )

    const { wrapper } = await mountSettings(fetchMock)
    await wrapper.find('[data-test="admin-toggle-2"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="admins-error"]').text()).toContain('свою учётную запись')
  })

  it('«Добавить администратора» открывает форму и создаёт учётную запись', async () => {
    fetchMock.mockImplementation(apiMock())

    const { wrapper } = await mountSettings(fetchMock)
    expect(wrapper.find('[data-test="admin-form-username"]').exists()).toBe(false)

    await wrapper.find('[data-test="admins-create"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="dialog"]').text()).toContain('Новый администратор')

    await wrapper.find('[data-test="admin-form-username"]').setValue('povar')
    await wrapper.find('[data-test="admin-form-password"]').setValue('povar-password-1')
    await wrapper.find('[data-test="admin-form-submit"]').trigger('click')
    await flushPromises()

    const postCall = fetchMock.mock.calls.find(
      (call) => String(call[0]) === '/api/v1/admin/admins' && call[1]?.method === 'POST',
    ) as [string, RequestInit]
    expect(bodyOf(postCall)).toEqual({ username: 'povar', password: 'povar-password-1' })
    expect(fetchMock.mock.calls.at(-1)?.[0]).toBe('/api/v1/admin/admins')
  })

  it('короткий пароль не отправляется: форма подсказывает длину (§11.6)', async () => {
    fetchMock.mockImplementation(apiMock())

    const { wrapper } = await mountSettings(fetchMock)
    await wrapper.find('[data-test="admins-create"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-test="admin-form-username"]').setValue('povar')
    await wrapper.find('[data-test="admin-form-password"]').setValue('short')
    await flushPromises()

    expect(wrapper.find('[data-test="admin-form-hint"]').exists()).toBe(true)
    expect(
      (wrapper.find('[data-test="admin-form-submit"]').element as HTMLButtonElement).disabled,
    ).toBe(true)
    expect(
      fetchMock.mock.calls.some((call) => call[1]?.method === 'POST'),
    ).toBe(false)
  })

  it('«Удалить» спрашивает подтверждение и удаляет после него (Д-8)', async () => {
    fetchMock.mockImplementation(apiMock())

    const { wrapper } = await mountSettings(fetchMock)
    await wrapper.find('[data-test="admin-delete-2"]').trigger('click')
    await flushPromises()
    const dialog = wrapper.find('[data-test="admin-delete-dialog"]')
    expect(dialog.exists()).toBe(true)
    expect(dialog.text()).toContain('Удалить администратора?')

    await wrapper.find('[data-test="admin-delete-confirm"]').trigger('click')
    await flushPromises()

    const deleteCall = fetchMock.mock.calls.find(
      (call) => String(call[0]) === '/api/v1/admin/admins/2' && call[1]?.method === 'DELETE',
    )
    expect(deleteCall).toBeDefined()
    expect(fetchMock.mock.calls.at(-1)?.[0]).toBe('/api/v1/admin/admins')
  })
})
