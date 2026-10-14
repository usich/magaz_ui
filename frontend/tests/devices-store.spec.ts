/**
 * Тесты стора оборудования подсети (Э4Б, Д-1, §11.7).
 *
 * Сеть подменяется `fetch`-моком, реальный сервер дашборда не требуется (§13).
 */

import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  checkTypeLabel,
  deviceStatusLabel,
  deviceStatusSeverity,
  deviceTypeLabel,
  useDevicesStore,
} from '@/stores/devices'

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
  message: 'Проверено устройств: 1, онлайн: 1',
  checked: 1,
  updated: 1,
  ignored: 0,
  results: [{ ip: '10.10.5.21', status: 'online', latency_ms: 3, error: null }],
}

describe('стор оборудования подсети', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    setActivePinia(createPinia())
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('загружает сводку и считает онлайн/офлайн/без данных', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse(
        makeMatrix([
          makeDevice(),
          makeDevice({ id: 2, ip_address: '10.10.5.22', last_status: 'offline', is_online: false }),
          makeDevice({ id: 3, ip_address: '10.10.5.23', last_status: null, is_online: false }),
        ]),
      ),
    )
    const devices = useDevicesStore()

    await devices.load()

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/admin/devices/matrix')
    expect(devices.rows).toHaveLength(3)
    expect(devices.total).toBe(3)
    expect(devices.online).toBe(1)
    expect(devices.offline).toBe(1)
    expect(devices.unknown).toBe(1)
    expect(devices.windowSec).toBe(180)
    expect(devices.lastLoadedAt).not.toBeNull()
  })

  it('передаёт фильтры в запрос матрицы (§11.7)', async () => {
    fetchMock.mockImplementation(async () => jsonResponse(makeMatrix([])))
    const devices = useDevicesStore()
    devices.storeId = 7
    devices.cluster = ' srv-1c '
    devices.deviceType = 'camera'
    devices.status = 'offline'
    devices.search = ' камера '
    devices.includeInactive = false

    await devices.load()

    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toContain('/admin/devices/matrix?')
    expect(url).toContain('store_id=7')
    expect(url).toContain('cluster=srv-1c')
    expect(url).toContain('device_type=camera')
    expect(url).toContain('status=offline')
    expect(url).toContain('search=%D0%BA%D0%B0%D0%BC%D0%B5%D1%80%D0%B0')
    expect(url).toContain('include_inactive=false')
  })

  it('«Проверить сейчас» по строке передаёт только её id', async () => {
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) =>
      init?.method === 'POST' ? jsonResponse(CHECK_RESULT) : jsonResponse(makeMatrix([])),
    )
    const devices = useDevicesStore()

    const result = await devices.check(5, [2])

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/admin/stores/5/devices/check')
    expect(init.method).toBe('POST')
    expect(String(init.body)).toContain('[2]')
    expect(result?.message).toContain('онлайн: 1')
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/admin/devices/matrix')
  })

  it('ошибку проверки показывает текстом и не ломает таблицу', async () => {
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) =>
      init?.method === 'POST'
        ? jsonResponse({ detail: 'Магазин недоступен' }, 503)
        : jsonResponse(makeMatrix([makeDevice()])),
    )
    const devices = useDevicesStore()

    const result = await devices.check(5)

    expect(result).toBeNull()
    expect(devices.errorMessage).toBe('Магазин недоступен')
    expect(devices.rows).toHaveLength(0)
  })

  it('отключение и возврат перечитывают сводку', async () => {
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) =>
      init?.method
        ? jsonResponse(makeDevice({ is_active: false, is_online: false }))
        : jsonResponse(makeMatrix([])),
    )
    const devices = useDevicesStore()

    await devices.deactivate(5, 1)
    await devices.restore(5, 1)

    const [deleteUrl, deleteInit] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(deleteUrl).toBe('/api/v1/admin/stores/5/devices/1')
    expect(deleteInit.method).toBe('DELETE')
    const [putUrl, putInit] = fetchMock.mock.calls[2] as [string, RequestInit]
    expect(putUrl).toBe('/api/v1/admin/stores/5/devices/1')
    expect(putInit.method).toBe('PUT')
    expect(String(putInit.body)).toContain('"is_active":true')
  })

  it('ошибка загрузки не стирает уже показанный список', async () => {
    fetchMock.mockImplementationOnce(async () => jsonResponse(makeMatrix([makeDevice()])))
    const devices = useDevicesStore()
    await devices.load()

    fetchMock.mockImplementationOnce(async () => jsonResponse({ detail: 'Сервис недоступен' }, 500))
    await devices.load()

    expect(devices.errorMessage).toBe('Сервис недоступен')
    expect(devices.rows).toHaveLength(1)
  })

  it('подписи и цвет статусов совпадают с контрактом агента (§4.11)', () => {
    expect(deviceStatusLabel(null)).toBe('нет данных')
    expect(deviceStatusLabel('offline')).toBe('офлайн')
    expect(deviceStatusSeverity('online')).toBe('success')
    expect(deviceStatusSeverity('offline')).toBe('danger')
    expect(deviceStatusSeverity(null)).toBe('secondary')
    expect(deviceTypeLabel('camera')).toBe('Камера')
    expect(deviceTypeLabel(null)).toBe('—')
    expect(checkTypeLabel('icmp', null)).toBe('ping (ICMP)')
    expect(checkTypeLabel('tcp', 80)).toBe('TCP-порт:80')
  })
})
