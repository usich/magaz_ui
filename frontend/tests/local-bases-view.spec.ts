/** Тесты отдельной страницы «Локальные базы» (Д-20). */

import { createPinia, type Pinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import LocalBasesView from '@/views/LocalBasesView.vue'

const toastAdd = vi.fn()
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }))

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
}

function makeBase(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 2, name: 'Бухгалтерия', version_1c: '8.3.24.1548', cluster_name: 'srv-1c',
    database_name: 'buh', user_name_1c: 'Администратор', password_set: true,
    credentials_version: 1, base_path: 'C:\\1C\\buh', extension_name: 'БГ_Бухгалтерия',
    group: 'Финансы', is_active: true, comment: null,
    backup_check_enabled: true, backup_path: null, backup_host: null, backup_use_auth: false,
    backup_user: null, backup_password_set: false, backup_check_yesterday: false,
    backup_status: 'ok', backup_error: null, backup_checked_at: null, backup_checked_date: '2026-10-23',
    backup_tries: 1, created_at: null, updated_at: null, ...overrides,
  }
}

const stubs = {
  Button: { props: ['label'], template: '<button>{{ label }}</button>' },
  Tag: { props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  Message: { template: '<div class="stub-msg"><slot /></div>' },
  InputText: { props: ['modelValue'], template: '<input />' },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /></div>' },
}

async function mountView(fetchMock: ReturnType<typeof vi.fn>): Promise<VueWrapper> {
  vi.stubGlobal('fetch', fetchMock)
  const pinia: Pinia = createPinia()
  const router: Router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/local-bases', name: 'local-bases', component: LocalBasesView }],
  })
  await router.push('/local-bases')
  await router.isReady()
  const wrapper = mount(LocalBasesView, { global: { plugins: [pinia, router], stubs } })
  await flushPromises()
  return wrapper
}

describe('страница «Локальные базы»', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('показывает группы и статус бэкапа (Д-20/Д-21)', async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/admin/local-bases')) {
        return jsonResponse([
          makeBase(),
          makeBase({ id: 3, name: 'Зарплата', database_name: 'zp', group: 'Финансы', backup_status: 'missing' }),
          makeBase({ id: 4, name: 'Торговля', database_name: 'trade', group: 'Продажи' }),
        ])
      }
      return jsonResponse([])
    })

    const wrapper = await mountView(fetchMock)

    expect(wrapper.find('[data-test="lb-group-Финансы"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="lb-group-Продажи"]').exists()).toBe(true)
    const finance = wrapper.find('[data-test="lb-group-Финансы"]')
    expect(finance.findAll('[data-test^="lb-row-"]')).toHaveLength(2)
    expect(wrapper.find('[data-test="lb-add"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('есть')
    expect(wrapper.text()).toContain('нет')
    wrapper.unmount()
  })

  it('кнопка «Добавить базу» открывает форму', async () => {
    fetchMock.mockImplementation(async () => jsonResponse([]))
    const wrapper = await mountView(fetchMock)

    await wrapper.find('[data-test="lb-add"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="local-base-dialog"]').exists()).toBe(true)
    wrapper.unmount()
  })
})
