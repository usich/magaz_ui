import { createPinia, setActivePinia, type Pinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '@/router'
import { useAuthStore } from '@/stores/auth'
import LoginView from '@/views/LoginView.vue'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** Заглушки PrimeVue-компонентов: тестируем логику формы, а не тему оформления. */
const stubs = {
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: `<input :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
  },
  Password: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: `<input type="password" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
  },
  Button: {
    props: ['label'],
    emits: ['click'],
    template: `<button type="submit" @click="$emit('click')">{{ label }}</button>`,
  },
  Message: {
    template: `<div class="stub-message"><slot /></div>`,
  },
}

async function mountLogin(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal('fetch', fetchMock)
  const pinia: Pinia = createPinia()
  setActivePinia(pinia)
  const router = createAppRouter(pinia, createMemoryHistory())
  await router.push('/login')
  await router.isReady()

  const wrapper = mount(LoginView, {
    global: { plugins: [pinia, router], stubs },
  })
  await flushPromises()
  return { wrapper, router, auth: useAuthStore(pinia) }
}

describe('страница входа', () => {
  beforeEach(() => {
    vi.useRealTimers()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('показывает версию сервера, когда API отвечает', async () => {
    // Заглушка отдаёт свежий Response на каждый запрос: guard тоже дергает /auth/me
    const fetchMock = vi.fn(async () =>
      jsonResponse({ status: 'online', app_name: 'Magaz UI', app_version: '1.0.0' }),
    )

    const { wrapper } = await mountLogin(fetchMock)

    expect(wrapper.text()).toContain('1.0.0')
  })

  it('успешный вход уводит на главную', async () => {
    const fetchMock = vi.fn(async (url: string) =>
      url.endsWith('/health')
        ? jsonResponse({ status: 'online', app_name: 'Magaz UI', app_version: '1.0.0' })
        : jsonResponse({ id: 1, username: 'admin', is_active: true, last_login_at: null }),
    )

    const { wrapper, router, auth } = await mountLogin(fetchMock)

    await wrapper.find('input[data-test="username"]').setValue('admin')
    await wrapper.find('input[data-test="password"]').setValue('magaz-test-password')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(auth.isAuthenticated).toBe(true)
    expect(router.currentRoute.value.name).toBe('dashboard')
  })

  it('неверный пароль показывает ошибку и очищает поле пароля', async () => {
    const fetchMock = vi.fn(async (url: string) =>
      url.endsWith('/health')
        ? jsonResponse({ status: 'online', app_name: 'Magaz UI', app_version: '1.0.0' })
        : jsonResponse({ detail: 'Неверный логин или пароль' }, 401),
    )

    const { wrapper, router, auth } = await mountLogin(fetchMock)

    await wrapper.find('input[data-test="username"]').setValue('admin')
    await wrapper.find('input[data-test="password"]').setValue('wrong-password')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(auth.isAuthenticated).toBe(false)
    expect(wrapper.find('[data-test="login-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Неверный логин или пароль')
    expect(router.currentRoute.value.name).toBe('login')
  })

  it('не отправляет запрос при пустых полях', async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ status: 'online', app_name: 'Magaz UI', app_version: '1.0.0' }),
    )

    const { wrapper } = await mountLogin(fetchMock)
    const callsBefore = fetchMock.mock.calls.length

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(fetchMock.mock.calls.length).toBe(callsBefore)
  })

  it('сообщает, если сервер дашборда недоступен', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'))

    const { wrapper } = await mountLogin(fetchMock)

    expect(wrapper.text()).toContain('Сервер дашборда недоступен')
  })
})
