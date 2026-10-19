/**
 * Тесты страницы «Уведомления» (§11.5, Э5, ФН3).
 *
 * PrimeVue-компоненты подменяются лёгкими заглушками: проверяем логику страницы
 * (прогресс «X из Y», фильтры, отмена рассылки, форма создания), а не оформление.
 * Сеть — `fetch`-мок, маршрут — память вместо истории.
 */

import { createPinia, type Pinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }))

import NotificationsView from '@/views/NotificationsView.vue'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function makeNotification(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 3,
    title: 'Смена закрывается в 22:00',
    body: 'Сохраните смену и отключите кассу',
    body_format: 'plain',
    delete_after_read: false,
    images: [],
    severity: 'important',
    requires_ack: true,
    target_type: 'all',
    target_value: null,
    deadline_at: null,
    repeat_every_min: null,
    category: null,
    is_active: true,
    state: 'active',
    created_by: 'admin',
    created_at: '2026-10-14T08:00:00Z',
    store_names: ['Магазин №5'],
    totals: {
      stores: 1,
      stores_delivered: 1,
      sessions: 3,
      receipts: 3,
      acknowledged: 1,
      shown: 1,
      snoozed: 1,
      expired: 0,
      pending: 0,
      percent: 33,
    },
    ...overrides,
  }
}

function makeList(items: Record<string, unknown>[]): Record<string, unknown> {
  return {
    items,
    total: items.length,
    counts: { active: items.length, done: 0, canceled: 0 },
    // Э9: счётчики вкладок сервер считает по всем уведомлениям, а не по фильтру
    severity_counts: { info: 1, important: items.length, critical: 0 },
    unread_by_severity: { info: 0, important: 2, critical: 0 },
    pending_receipts: 2,
    snooze_min: 15,
  }
}

const REPORT = {
  notification: makeNotification(),
  state: 'active',
  deadline_at: null,
  created_at: '2026-10-14T08:00:00Z',
  totals: makeNotification().totals,
  snooze_min: 15,
  stores: [],
}

/**
 * Мок админского API: список, отмена, карточка отчёта и магазины (нужны фильтру).
 * Ветки идут от частных адресов к общим — так `/notifications/3` не «съедается»
 * проверкой списка.
 */
function apiMock(
  list: unknown,
  stores: unknown[] = [{ id: 5, name: 'Магазин №5', cluster_name: 'srv-1c' }],
): (url: string, init?: RequestInit) => Promise<Response> {
  return async (url: string) => {
    if (url.includes('/cancel')) {
      return jsonResponse({
        notification_id: 3,
        state: 'canceled',
        canceled_jobs: 1,
        message: 'Рассылка остановлена',
      })
    }
    if (url.includes('/admin/notifications/')) return jsonResponse(REPORT)
    if (url.includes('/admin/notifications')) return jsonResponse(list)
    return jsonResponse(stores)
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
  // Редактор с TipTap в jsdom не поднимаем: его контракт проверяет
  // `notification-editor.spec.ts`, а здесь важна логика страницы и формы (§11.5).
  RichTextEditor: {
    props: ['modelValue', 'drafts'],
    emits: ['update:modelValue', 'update:drafts'],
    template: '<div>{{ modelValue }}</div>',
  },
}
async function mountNotifications(
  fetchMock: ReturnType<typeof vi.fn>,
  path = '/notifications',
): Promise<{ wrapper: VueWrapper; router: Router; pinia: Pinia }> {
  vi.stubGlobal('fetch', fetchMock)
  const pinia: Pinia = createPinia()
  const router: Router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/notifications', name: 'notifications', component: NotificationsView },
      { path: '/notifications/:id(\\d+)', name: 'notification-detail', component: { template: '<div />' } },
      { path: '/stores/:id(\\d+)', name: 'store-detail', component: { template: '<div />' } },
    ],
  })
  await router.push(path)
  await router.isReady()

  const wrapper = mount(NotificationsView, { global: { plugins: [pinia, router], stubs } })
  await flushPromises()
  return { wrapper, router, pinia }
}

describe('страница «Уведомления»', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('показывает строку с прогрессом «X из Y» и ссылкой на CSV (§11.5)', async () => {
    fetchMock.mockImplementation(apiMock(makeList([makeNotification()])))

    const { wrapper } = await mountNotifications(fetchMock)

    // Первым страница читает магазины (нужны фильтру), поэтому ищем свой запрос
    const listCall = fetchMock.mock.calls.find((call) =>
      String(call[0]).includes('/admin/notifications'),
    )
    expect(listCall?.[0]).toBe('/api/v1/admin/notifications')

    const row = wrapper.find('[data-test="notification-row-3"]')
    expect(row.exists()).toBe(true)
    expect(row.text()).toContain('Смена закрывается в 22:00')
    expect(wrapper.find('[data-test="notification-progress-3"]').text()).toContain('1 из 3')
    expect(wrapper.find('[data-test="notification-progress-3"]').text()).toContain('отложили 1')
    expect(wrapper.find('[data-test="notification-state-3"]').text()).toBe('в работе')
    expect(wrapper.find('[data-test="notification-severity-3"]').text()).toBe('важно')
    expect(wrapper.find('[data-test="notification-csv-3"]').attributes('href')).toBe(
      '/api/v1/admin/notifications/3/report?format=csv',
    )
    expect(wrapper.find('[data-test="notifications-pending"]').text()).toBe('2')
    expect(wrapper.find('[data-test="notifications-snooze"]').text()).toBe('15 мин')
  })

  it('фильтр «в работе» из адреса плитки главной применяется при загрузке (§11.1)', async () => {
    fetchMock.mockImplementation(apiMock(makeList([])))

    const { wrapper } = await mountNotifications(fetchMock, '/notifications?status=active')

    const listCall = fetchMock.mock.calls.find((call) =>
      String(call[0]).includes('/admin/notifications'),
    ) as [string]
    const url = listCall[0]
    expect(url).toContain('/admin/notifications?')
    expect(url).toContain('status=active')
    expect(
      (wrapper.find('[data-test="notifications-status-filter"]').element as HTMLSelectElement).value,
    ).toBe('active')
  })

  it('кнопка «Создать уведомление» открывает форму (§11.5)', async () => {
    fetchMock.mockImplementation(apiMock(makeList([])))

    const { wrapper } = await mountNotifications(fetchMock)

    expect(wrapper.find('[data-test="dialog"]').exists()).toBe(false)
    await wrapper.find('[data-test="notifications-create"]').trigger('click')
    await flushPromises()

    const dialog = wrapper.find('[data-test="dialog"]')
    expect(dialog.exists()).toBe(true)
    expect(dialog.text()).toContain('Новое уведомление')
    expect(wrapper.find('[data-test="notify-form-title"]').exists()).toBe(true)
  })

  it('«Отменить» отправляет POST и перечитывает список (§11.5)', async () => {
    fetchMock.mockImplementation(apiMock(makeList([makeNotification()])))

    const { wrapper } = await mountNotifications(fetchMock)
    await wrapper.find('[data-test="notification-cancel-3"]').trigger('click')
    await flushPromises()

    const cancelCall = fetchMock.mock.calls.find((call) =>
      String(call[0]).includes('/cancel'),
    ) as [string, RequestInit]
    expect(cancelCall[0]).toBe('/api/v1/admin/notifications/3/cancel')
    expect(cancelCall[1].method).toBe('POST')
    expect(fetchMock.mock.calls.at(-1)?.[0]).toBe('/api/v1/admin/notifications')
  })

  it('после отмены кнопки «Отменить» больше нет', async () => {
    fetchMock.mockImplementation(
      apiMock(makeList([makeNotification({ is_active: false, state: 'canceled' })])),
    )

    const { wrapper } = await mountNotifications(fetchMock)

    expect(wrapper.find('[data-test="notification-cancel-3"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="notification-state-3"]').text()).toBe('отменено')
  })

  it('пустой список объясняет, что доставку выполняет диспетчер', async () => {
    fetchMock.mockImplementation(apiMock(makeList([])))

    const { wrapper } = await mountNotifications(fetchMock)

    expect(wrapper.find('[data-test="notifications-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="notifications-empty"]').text()).toContain('диспетчер')
  })

  it('вкладки важности показывают счётчики и «ждут ознакомления» (Э9, §11.5)', async () => {
    fetchMock.mockImplementation(apiMock(makeList([makeNotification()])))

    const { wrapper } = await mountNotifications(fetchMock)

    expect(wrapper.find('[data-test="notifications-tab-all"]').text()).toContain('(1)')
    const important = wrapper.find('[data-test="notifications-tab-important"]').text()
    expect(important).toContain('важно')
    expect(important).toContain('(1)')
    expect(important).toContain('ждут ознакомления 2')
    expect(wrapper.find('[data-test="notifications-tab-critical"]').text()).toContain('(0)')
  })

  it('вкладка важности меняет фильтр и перечитывает список (Э9)', async () => {
    fetchMock.mockImplementation(apiMock(makeList([makeNotification()])))

    const { wrapper } = await mountNotifications(fetchMock)
    await wrapper.find('[data-test="notifications-tab-critical"]').trigger('click')
    await flushPromises()

    const lastCall = fetchMock.mock.calls.at(-1) as [string]
    expect(lastCall[0]).toContain('severity=critical')
  })

  it('строка списка показывает текст разметки и признаки Э9 (§11.5)', async () => {
    fetchMock.mockImplementation(
      apiMock(
        makeList([
          makeNotification({
            body: '<p>Смена <b>закрывается</b></p><ul><li>Сохраните смену</li></ul>',
            body_format: 'html',
            images: ['shema.png'],
            delete_after_read: true,
          }),
        ]),
      ),
    )

    const { wrapper } = await mountNotifications(fetchMock)
    const row = wrapper.find('[data-test="notification-row-3"]')
    const rich = wrapper.find('[data-test="notification-rich-3"]')

    expect(row.text()).toContain('Смена закрывается')
    expect(row.text()).not.toContain('<b>')
    expect(rich.text()).toContain('картинок 1')
    expect(rich.text()).toContain('удаляется после прочтения')
  })

  it('форма даёт редактор, предпросмотр и «удалять после прочтения» (Э9)', async () => {
    fetchMock.mockImplementation(apiMock(makeList([])))

    const { wrapper } = await mountNotifications(fetchMock)
    await wrapper.find('[data-test="notifications-create"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="notify-form-editor"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="notify-form-delete-after-read"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="notify-form-preview"]').exists()).toBe(true)
  })
})

