/**
 * Тесты Э4 на фронтенде: стор задач, стор матрицы файлов и выбор получателя
 * операции (§11.3, §11.4, §11.6).
 *
 * Сеть подменяется `fetch`-моком, как и в остальных тестах SPA (§13):
 * проверяется то, что уходит на сервер, — URL, метод и тело запроса.
 */

import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { dispatchAction } from '@/api/actions'
import { useFilesStore } from '@/stores/files'
import { useJobsStore } from '@/stores/jobs'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function makeJob(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 7,
    store_id: 1,
    store_name: 'Магазин №5',
    kind: 'run_merge',
    status: 'queued',
    params: { file_name: 'update' },
    attempts: 0,
    last_error: null,
    log_files: [],
    created_by: 'admin',
    created_at: '2026-10-13T10:00:00Z',
    started_at: null,
    finished_at: null,
    last_message: 'Задача создана администратором admin',
    excerpt_preview: null,
    ...overrides,
  }
}

function makeRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    store_id: 1,
    store_name: 'Магазин №5',
    ip_address: '10.10.5.10',
    cluster_name: 'srv-1c',
    is_active: true,
    is_online: true,
    filename: 'update',
    checked_at: '2026-10-13T10:05:00Z',
    cf_exists: true,
    cfe_exists: false,
    xml_exists: true,
    cf_size: 2048,
    cfe_size: null,
    xml_size: 512,
    cf_mtime: '2026-10-13T09:00:00Z',
    cfe_mtime: null,
    xml_mtime: null,
    has_file: true,
    checked: true,
    ...overrides,
  }
}

describe('стор задач (§11.6)', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    setActivePinia(createPinia())
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('загружает страницу задач со счётчиками', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse({
        items: [makeJob()],
        counts: { queued: 1, success: 2 },
        total: 3,
        active: 1,
      }),
    )
    const jobs = useJobsStore()

    await jobs.load()

    expect(jobs.items).toHaveLength(1)
    expect(jobs.counts.success).toBe(2)
    expect(jobs.total).toBe(3)
    expect(jobs.active).toBe(1)
    expect(jobs.lastLoadedAt).not.toBeNull()
  })

  it('передаёт фильтры в запрос истории', async () => {
    fetchMock.mockImplementation(async () => jsonResponse({ items: [], counts: {}, total: 0, active: 0 }))
    const jobs = useJobsStore()
    jobs.storeId = 5
    jobs.kind = 'run_merge'
    jobs.status = 'failed'
    jobs.activeOnly = true

    await jobs.load()

    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toContain('/admin/jobs?')
    expect(url).toContain('store_id=5')
    expect(url).toContain('kind=run_merge')
    expect(url).toContain('status=failed')
    expect(url).toContain('active_only=true')
  })

  it('карточка задачи тянет полный журнал', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse({
        ...makeJob({ status: 'success' }),
        logs: [{ id: 1, level: 'info', message: 'старт', created_at: null }],
      }),
    )
    const jobs = useJobsStore()

    const detail = await jobs.loadDetail(7)

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/admin/jobs/7')
    expect(detail?.logs[0].message).toBe('старт')
    expect(jobs.errorMessage).toBe('')
  })

  it('ошибка загрузки не ломает стор', async () => {
    fetchMock.mockImplementation(async () => jsonResponse({ detail: 'Не найдено' }, 404))
    const jobs = useJobsStore()

    const detail = await jobs.loadDetail(7)

    expect(detail).toBeNull()
    expect(jobs.errorMessage).toBe('Не найдено')
  })
})

describe('стор матрицы файлов (§11.3)', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    setActivePinia(createPinia())
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('загружает матрицу и считает «есть у N из M»', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse({
        filename: 'update',
        rows: [makeRow(), makeRow({ store_id: 2, has_file: false, cf_exists: false })],
        total: 2,
        present: 1,
        missing_current: 1,
      }),
    )
    const files = useFilesStore()

    await files.load()

    expect(fetchMock.mock.calls[0][0]).toContain('/admin/files/matrix?')
    expect(files.total).toBe(2)
    expect(files.present).toBe(1)
    expect(files.missing).toBe(1)
    expect(files.clusters).toEqual(['srv-1c'])
  })

  it('выбор строк и «выбрать все»', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse({
        filename: 'update',
        rows: [makeRow(), makeRow({ store_id: 2 })],
        total: 2,
        present: 2,
        missing_current: 0,
      }),
    )
    const files = useFilesStore()
    await files.load()

    files.toggle(1)
    expect(files.selected).toEqual([1])
    expect(files.allSelected).toBe(false)

    files.toggleAll()
    expect(files.selected).toEqual([1, 2])
    expect(files.allSelected).toBe(true)

    files.toggleAll()
    expect(files.selected).toEqual([])
  })
})

describe('выбор получателя операции (§11.4)', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    setActivePinia(createPinia())
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('один магазин — одиночная точка `/stores/{id}/actions`', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse({
        job: makeJob(),
        message: 'Задача #7 поставлена в очередь',
        warning: null,
      }),
    )

    const outcome = await dispatchAction({ storeIds: [1] }, 'run_merge', { file_name: 'update' })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/admin/stores/1/actions')
    expect(init.method).toBe('POST')
    expect(String(init.body)).toContain('run_merge')
    expect(outcome.jobs[0].job_id).toBe(7)
    expect(outcome.warning).toBeNull()
  })

  it('несколько магазинов — массовая точка с `store_ids`', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse({
        action: 'check_files',
        message: 'Задачи поставлены: 2 из 3 магазинов',
        total: 3,
        jobs: [
          { store_id: 1, store_name: 'Магазин №5', job_id: 8, status: 'queued', detail: null },
          { store_id: 2, store_name: 'Магазин №6', job_id: 9, status: 'queued', detail: null },
          { store_id: 3, store_name: 'Магазин №7', job_id: null, status: 'skipped', detail: 'отключён' },
        ],
        warning: 'На кассе нет файла update.cf',
      }),
    )

    const outcome = await dispatchAction({ storeIds: [1, 2, 3] }, 'check_files', {
      filename: 'update',
    })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/admin/bulk/actions')
    expect(String(init.body)).toContain('"store_ids":[1,2,3]')
    expect(outcome.skipped).toBe(1)
    expect(outcome.warning).toContain('update.cf')
  })

  it('«всем» отправляет флаг `all`, а не пустой список', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse({
        action: 'run_update_db',
        message: 'Задачи поставлены: 3 из 3 магазинов',
        total: 3,
        jobs: [],
        warning: null,
      }),
    )

    await dispatchAction({ all: true }, 'run_update_db', { is_fast_update: false })

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(String(init.body)).toContain('"all":true')
    expect(String(init.body)).toContain('"is_fast_update":false')
  })

  it('«разослать ссылку» уходит массовым `download_file` (ФН2)', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse({
        action: 'download_file',
        message: 'Задачи поставлены: 2 из 2 магазинов',
        total: 2,
        jobs: [],
        warning: null,
      }),
    )
    const files = useFilesStore()

    await files.sendLink('https://disk.yandex.ru/d/XXXX/update.cf', 'all')

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/admin/bulk/actions')
    expect(String(init.body)).toContain('download_file')
    expect(String(init.body)).toContain('url-file')
    expect(files.lastOutcome?.message).toContain('2 из 2')
  })
})