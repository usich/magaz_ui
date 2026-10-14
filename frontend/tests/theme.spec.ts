/**
 * Тесты темы оформления (§11.1, Э8).
 *
 * Проверяем стор (`stores/theme.ts`) и переключатель в шапке (`AppShell`): класс
 * `.app-dark` на <html>, сохранение выбора в `localStorage` и реакцию на смену
 * системной настройки в режиме «как в системе».
 */

import { createPinia, setActivePinia, type Pinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }))

// Оболочка использует `useRoute`/`useRouter`, ссылки и PrimeVue-кнопки:
// подменяем всё это лёгкими заглушками, чтобы тема проверялась изолированно
vi.mock('vue-router', () => ({
  useRoute: () => ({ meta: { title: 'Магазины' } }),
  useRouter: () => ({ push: vi.fn() }),
}))

import AppShell from '@/components/AppShell.vue'
import { DARK_CLASS, THEME_STORAGE_KEY, useThemeStore } from '@/stores/theme'

/** Управляемая замена `matchMedia`: позволяет «переключить» настройку ОС в тесте */
function stubMatchMedia(matches: boolean) {
  const listeners: ((event: { matches: boolean }) => void)[] = []
  const media = {
    matches,
    media: '(prefers-color-scheme: dark)',
    onchange: null,
    addEventListener: (_event: string, listener: (event: { matches: boolean }) => void) => {
      listeners.push(listener)
    },
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }
  vi.stubGlobal('matchMedia', vi.fn(() => media))
  return { media, listeners }
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** Минимальные заглушки компонентов, которые использует оболочка */
const stubs = {
  Button: { props: ['label'], template: '<button>{{ label }}</button>' },
  Toast: true,
  RouterLink: true,
  RouterView: true,
}

describe('тема оформления', () => {
  let pinia: Pinia

  beforeEach(() => {
    window.localStorage.clear()
    document.documentElement.className = ''
    delete document.documentElement.dataset.theme
    pinia = createPinia()
    setActivePinia(pinia)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('по умолчанию следует настройке ОС', () => {
    stubMatchMedia(true)
    const theme = useThemeStore()
    theme.initialize()

    expect(theme.mode).toBe('system')
    expect(theme.followsSystem, 'без выбора в localStorage тема — системная').toBe(true)
    expect(theme.isDark).toBe(true)
    expect(document.documentElement.classList.contains(DARK_CLASS)).toBe(true)
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('явный выбор перекрывает системную настройку и сохраняется', () => {
    stubMatchMedia(true)
    const theme = useThemeStore()
    theme.initialize()

    theme.setMode('light')

    expect(theme.isDark).toBe(false)
    expect(document.documentElement.classList.contains(DARK_CLASS)).toBe(false)
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')

    theme.toggle()
    expect(theme.mode).toBe('dark')
    expect(document.documentElement.classList.contains(DARK_CLASS)).toBe(true)
  })

  it('в режиме «как в системе» реагирует на смену настройки ОС', () => {
    const { listeners } = stubMatchMedia(false)
    const theme = useThemeStore()
    theme.initialize()
    expect(theme.isDark).toBe(false)

    listeners.forEach((listener) => listener({ matches: true }))

    expect(theme.systemPrefersDark).toBe(true)
    expect(document.documentElement.classList.contains(DARK_CLASS)).toBe(true)
  })

  it('в явном режиме смена настройки ОС ничего не меняет', () => {
    const { listeners } = stubMatchMedia(false)
    const theme = useThemeStore()
    theme.initialize()
    theme.setMode('light')

    listeners.forEach((listener) => listener({ matches: true }))

    expect(theme.isDark).toBe(false)
    expect(document.documentElement.classList.contains(DARK_CLASS)).toBe(false)
  })

  it('переключатель в шапке меняет тему и сохраняет выбор', async () => {
    stubMatchMedia(false)
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ app_version: '1.0.0', spa: { built: true }, counts: {} })),
    )

    const wrapper = mount(AppShell, { global: { plugins: [pinia], stubs } })
    await flushPromises()

    const select = wrapper.find('[data-test="theme-mode"]')
    expect(select.exists()).toBe(true)

    await select.setValue('dark')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
    expect(document.documentElement.classList.contains(DARK_CLASS)).toBe(true)

    await select.setValue('light')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')
    expect(document.documentElement.classList.contains(DARK_CLASS)).toBe(false)

    wrapper.unmount()
  })
})
