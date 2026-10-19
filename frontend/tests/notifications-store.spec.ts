/**
 * Тесты стора уведомлений (Э5, ФН3, §11.5).
 *
 * Сеть подменяется `fetch`-моком, реальный сервер дашборда не требуется (§13).
 * Проверяется то, за что отвечает стор: URL и фильтры, прогресс «X из Y» из
 * ответа сервера, отмена рассылки и ссылка на CSV-выгрузку.
 */

import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  RECEIPT_LABELS,
  SEVERITY_LABELS,
  STATE_LABELS,
  receiptLabel,
  receiptTone,
  severityTone,
  stateLabel,
  targetTypeLabel,
  useNotificationsStore,
} from '@/stores/notifications'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function makeTotals(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    stores: 2,
    stores_delivered: 2,
    sessions: 3,
    receipts: 3,
    acknowledged: 1,
    shown: 1,
    snoozed: 1,
    expired: 0,
    pending: 0,
    percent: 33,
    ...overrides,
  }
}

function makeNotification(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 3,
    title: 'Смена закрывается в 22:00',
    body: 'Сохраните смену и отключите кассу',
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
    store_names: ['Магазин №5', 'Магазин №6'],
    totals: makeTotals(),
    ...overrides,
  }
}

function makeList(items: Record<string, unknown>[]): Record<string, unknown> {
  return {
    items,
    total: items.length,
    counts: { active: 1, done: 0, canceled: 0 },
    pending_receipts: 2,
    snooze_min: 15,
  }
}

function makeReport(): Record<string, unknown> {
  return {
    notification: makeNotification(),
    state: 'active',
    deadline_at: null,
    created_at: '2026-10-14T08:00:00Z',
    totals: makeTotals(),
    snooze_min: 15,
    stores: [
      {
        store_id: 5,
        store_name: 'Магазин №5',
        cluster_name: 'srv-1c',
        store_is_active: true,
        delivered_at: '2026-10-14T08:00:05Z',
        sessions_count: 2,
        total: 2,
        acknowledged: 1,
        receipts: [
          {
            session_user: 'user1',
            session_id: '1',
            status: 'acknowledged',
            shown_at: '2026-10-14T08:00:10Z',
            acknowledged_at: '2026-10-14T08:01:00Z',
            ack_comment: 'принял',
            snooze_count: 0,
            snooze_until: null,
            is_acknowledged: true,
            is_overdue: false,
          },
          {
            session_user: 'user2',
            session_id: '2',
            status: 'snoozed',
            shown_at: '2026-10-14T08:00:10Z',
            acknowledged_at: null,
            ack_comment: null,
            snooze_count: 1,
            snooze_until: '2026-10-14T08:15:10Z',
            is_acknowledged: false,
            is_overdue: false,
          },
        ],
      },
    ],
  }
}


describe('стор уведомлений', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    setActivePinia(createPinia())
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('загружает список с прогрессом и счётчиками (§11.5)', async () => {
    fetchMock.mockImplementation(async () => jsonResponse(makeList([makeNotification()])))
    const notifications = useNotificationsStore()

    await notifications.load()

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/admin/notifications')
    expect(notifications.items).toHaveLength(1)
    expect(notifications.total).toBe(1)
    expect(notifications.counts.active).toBe(1)
    expect(notifications.pendingReceipts).toBe(2)
    expect(notifications.snoozeMin).toBe(15)
    expect(notifications.items[0].totals.acknowledged).toBe(1)
    expect(notifications.lastLoadedAt).not.toBeNull()
  })

  it('передаёт фильтры в запрос списка (§11.5)', async () => {
    fetchMock.mockImplementation(async () => jsonResponse(makeList([])))
    const notifications = useNotificationsStore()
    notifications.status = 'canceled'
    notifications.severity = 'critical'
    notifications.search = ' смена '
    notifications.storeId = 7

    await notifications.load()

    const url = fetchMock.mock.calls[0][0] as string
    expect(url).toContain('/admin/notifications?')
    expect(url).toContain('status=canceled')
    expect(url).toContain('severity=critical')
    expect(url).toContain('search=%D1%81%D0%BC%D0%B5%D0%BD%D0%B0')
    expect(url).toContain('store_id=7')
  })

  it('создаёт уведомление, перечитывает список и возвращает ответ (§11.5)', async () => {
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) =>
      init?.method === 'POST'
        ? jsonResponse({
            notification: makeNotification({ id: 9 }),
            stores: 2,
            jobs: 2,
            skipped_store_ids: [],
            message: 'Уведомление создано и поставлено в рассылку',
          })
        : jsonResponse(makeList([makeNotification({ id: 9 })])),
    )
    const notifications = useNotificationsStore()

    const response = await notifications.create({
      title: 'Смена закрывается',
      body: 'Сохраните смену',
      severity: 'critical',
      requires_ack: true,
      target_type: 'stores',
      store_ids: [5, 6],
      category_id: null,
    })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/admin/notifications')
    expect(init.method).toBe('POST')
    expect(String(init.body)).toContain('"target_type":"stores"')
    expect(String(init.body)).toContain('[5,6]')
    expect(response?.jobs).toBe(2)
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/admin/notifications')
    expect(notifications.isSaving).toBe(false)
  })

  it('ошибка создания показывается текстом и не бросает исключение', async () => {
    fetchMock.mockImplementation(async () => jsonResponse({ detail: 'Нет магазинов' }, 422))
    const notifications = useNotificationsStore()

    const response = await notifications.create({
      title: 'x',
      body: 'y',
      severity: 'info',
      requires_ack: false,
      target_type: 'all',
      category_id: null,
    })

    expect(response).toBeNull()
    expect(notifications.errorMessage).toBe('Нет магазинов')
  })

  it('отмена рассылки перечитывает список и отчёт, если он открыт', async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/cancel')) {
        return jsonResponse({
          notification_id: 3,
          state: 'canceled',
          canceled_jobs: 1,
          message: 'Рассылка остановлена',
        })
      }
      if (url.includes('/notifications/3')) return jsonResponse(makeReport())
      return jsonResponse(makeList([makeNotification({ is_active: false, state: 'canceled' })]))
    })
    const notifications = useNotificationsStore()
    await notifications.loadReport(3)

    const ok = await notifications.cancel(3)

    expect(ok).toBe(true)
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/admin/notifications/3/cancel')
    expect(fetchMock.mock.calls[1][1]?.method).toBe('POST')
    expect(notifications.items[0].is_active).toBe(false)
  })

  it('отчёт по уведомлению доступен странице карточки (§11.5)', async () => {
    fetchMock.mockImplementation(async () => jsonResponse(makeReport()))
    const notifications = useNotificationsStore()

    const report = await notifications.loadReport(3)

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/admin/notifications/3')
    expect(report?.stores[0].receipts).toHaveLength(2)
    expect(notifications.report?.snooze_min).toBe(15)
  })

  it('ошибка отчёта очищает отчёт и объясняет причину', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse({ detail: 'Уведомление не найдено' }, 404),
    )
    const notifications = useNotificationsStore()

    const report = await notifications.loadReport(404)

    expect(report).toBeNull()
    expect(notifications.report).toBeNull()
    expect(notifications.errorMessage).toBe('Уведомление не найдено')
  })

  it('ссылка на CSV ведёт на серверный отчёт с `format=csv` (§11.5)', () => {
    const notifications = useNotificationsStore()

    expect(notifications.csvUrl(3)).toBe('/api/v1/admin/notifications/3/report?format=csv')
  })

  it('подписи и цвета статусов совпадают с контрактом сервера (§4.8, П-2)', () => {
    expect(receiptLabel(null)).toBe(RECEIPT_LABELS.pending)
    expect(receiptLabel('acknowledged')).toBe('ознакомлен')
    expect(receiptLabel('snoozed')).toBe('отложено')
    expect(receiptTone('acknowledged')).toBe('success')
    expect(receiptTone('snoozed')).toBe('warn')
    expect(receiptTone('expired')).toBe('danger')
    expect(severityTone('critical')).toBe('danger')
    expect(severityTone('important')).toBe('warn')
    expect(SEVERITY_LABELS.critical).toBe('критично')
    expect(STATE_LABELS.active).toBe('в работе')
    expect(stateLabel('canceled')).toBe('отменено')
  })

  it('охват рассылки показывается человеческим языком (§11.5)', () => {
    expect(targetTypeLabel(makeNotification() as never)).toContain('все магазины')
    expect(
      targetTypeLabel(
        makeNotification({ target_type: 'cluster', target_value: 'srv-1c' }) as never,
      ),
    ).toBe('кластер srv-1c')
    expect(
      targetTypeLabel(
        makeNotification({ target_type: 'stores', store_names: ['Магазин №5'] }) as never,
      ),
    ).toBe('Магазин №5')
  })
})
