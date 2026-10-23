/** Тесты страницы «Обновление 1С» (§Д-19): вкладки, статусы merge/update, журнал. */

import { createPinia, type Pinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import UpdatesView from '@/views/UpdatesView.vue'

const toastAdd = vi.fn()
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }))

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function makeStore(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 1, name: 'Магазин №5', ip_address: '10.10.5.10', agent_port: 8383,
    cluster_name: 'srv-1c', database_name: 'trade', version_1c: '8.3.24.1548',
    user_name_1c: 'Администратор', password_set: true, credentials_version: 1,
    devices_version: 1, agent_version: '1.0.0', os_info: null,
    last_seen_at: '2026-10-13T10:00:00Z', is_online: true, is_active: true,
    comment: null, extension_name: null, created_at: null, updated_at: null, ...overrides,
  }
}

function makeJob(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 7, store_id: 1, store_name: 'Магазин №5', local_base_id: null, local_base_name: null,
    kind: 'run_merge', status: 'success',
    params: {}, attempts: 0, last_error: null, log_files: [],
    created_by: 'admin', created_at: '2026-10-13T10:00:00Z',
    started_at: '2026-10-13T10:00:01Z', finished_at: '2026-10-13T10:00:10Z',
    last_message: null, excerpt_preview: null, ...overrides,
  }
}

function makeMatrixRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    store_id: 1, store_name: 'Магазин №5', ip_address: '10.10.5.10', cluster_name: 'srv-1c',
    is_active: true, is_online: true, filename: 'update', checked_at: '2026-10-13T10:05:00Z',
    cf_exists: true, cfe_exists: false, xml_exists: true,
    cf_size: 1024, cfe_size: null, xml_size: 512, ...overrides,
  }
}

function makeLocalBase(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 2, name: 'Бухгалтерия', version_1c: '8.3.24.1548', cluster_name: 'srv-1c',
    database_name: 'buh', user_name_1c: 'Администратор', password_set: true,
    credentials_version: 1, base_path: 'C:\\1C\\buh', extension_name: 'БГ_Бухгалтерия',
    group: 'Финансы', is_active: true, comment: null, created_at: null, updated_at: null, ...overrides,
  }
}

function apiMock(
  stores: unknown[],
  rows: unknown[],
  merge: unknown[],
  upd: unknown[],
  recent: unknown[],
  localBases: unknown[] = [],
  localJobs: unknown[] = [],
  localMergeJobs: unknown[] = [],
  localCheckJobs: unknown[] = [],
  onRunUpdate?: (body: unknown) => void,
  onBulkUpdate?: (body: unknown) => void,
  onCheckFiles?: (body: unknown) => void,
  onRunMerge?: (body: unknown) => void,
) {
  return async (url: string, init: RequestInit = {}): Promise<Response> => {
    if (init.method === 'POST' && url.includes('/check-files')) {
      onCheckFiles?.(init.body)
      return jsonResponse(
        {
          message: 'Проверка файлов базы поставлена в очередь',
          job: { id: 12, local_base_id: 2, local_base_name: 'Бухгалтерия', kind: 'check_files_local', status: 'queued', created_at: '2026-10-13T10:00:00Z' },
        },
        202,
      )
    }
    if (init.method === 'POST' && url.includes('/run-merge')) {
      onRunMerge?.(init.body)
      return jsonResponse(
        {
          message: 'Слияние базы поставлено в очередь',
          job: { id: 13, local_base_id: 2, local_base_name: 'Бухгалтерия', kind: 'run_merge_local', status: 'queued', created_at: '2026-10-13T10:00:00Z' },
        },
        202,
      )
    }
    if (init.method === 'POST' && url.includes('/bulk/update')) {
      onBulkUpdate?.(init.body)
      return jsonResponse(
        {
          message: 'Задачи поставлены: 2 из 2 баз',
          total: 2,
          jobs: [
            { local_base_id: 2, local_base_name: 'Бухгалтерия', job_id: 10, status: 'queued', detail: null },
            { local_base_id: 3, local_base_name: 'Торговля', job_id: 11, status: 'queued', detail: null },
          ],
        },
        202,
      )
    }
    if (init.method === 'POST' && url.includes('/run-update')) {
      onRunUpdate?.(init.body)
      return jsonResponse(
        {
          message: 'Обновление базы поставлено в очередь',
          job: { id: 9, local_base_id: 2, local_base_name: 'Бухгалтерия', kind: 'run_update_db_local', status: 'queued', created_at: '2026-10-13T10:00:00Z' },
        },
        202,
      )
    }
    if (url.includes('/admin/stores')) return jsonResponse(stores)
    if (url.includes('/admin/local-bases')) return jsonResponse(localBases)
    if (url.includes('/admin/files/matrix')) return jsonResponse({ rows, total: rows.length, present: 1, missing: 0 })
    if (url.includes('/admin/jobs')) {
      if (url.includes('kind=run_merge_local')) return jsonResponse({ items: localMergeJobs, counts: {}, total: 0, active: 0 })
      if (url.includes('kind=check_files_local')) return jsonResponse({ items: localCheckJobs, counts: {}, total: 0, active: 0 })
      if (url.includes('kind=run_update_db_local')) return jsonResponse({ items: localJobs, counts: {}, total: 0, active: 0 })
      if (url.includes('kind=run_merge')) return jsonResponse({ items: merge, counts: {}, total: 0, active: 0 })
      if (url.includes('kind=run_update_db')) return jsonResponse({ items: upd, counts: {}, total: 0, active: 0 })
      return jsonResponse({ items: recent, counts: {}, total: 0, active: 0 })
    }
    return jsonResponse({ detail: 'не найдено' }, 404)
  }
}

const stubs = {
  Button: { props: ['label', 'disabled'], template: '<button :disabled="disabled">{{ label }}</button>' },
  Tag: { props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  Checkbox: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input type="checkbox" :checked="modelValue" @change="$emit(\'update:modelValue\', $event.target.checked)" />',
  },
  RadioButton: {
    props: ['modelValue', 'value'],
    emits: ['update:modelValue'],
    template: '<button @click="$emit(\'update:modelValue\', value)" />',
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /><slot name="footer" /></div>' },
  OperationPanel: { template: '<div data-test="operation-panel" />' },
}

async function mountUpdates(fetchMock: ReturnType<typeof vi.fn>): Promise<VueWrapper> {
  vi.stubGlobal('fetch', fetchMock)
  const pinia: Pinia = createPinia()
  const router: Router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/updates', name: 'updates', component: UpdatesView },
      { path: '/stores/:id(\\d+)', name: 'store-detail', component: { template: '<div />' } },
    ],
  })
  await router.push('/updates')
  await router.isReady()
  const wrapper = mount(UpdatesView, { global: { plugins: [pinia, router], stubs } })
  await flushPromises()
  return wrapper
}

describe('страница «Обновление 1С»', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('вкладки, таблица магазинов со статусами merge/update и журнал (Д-19)', async () => {
    fetchMock.mockImplementation(
      apiMock(
        [makeStore()],
        [makeMatrixRow()],
        [makeJob({ kind: 'run_merge', status: 'success' })],
        [makeJob({ kind: 'run_update_db', status: 'running' })],
        [
          makeJob({ kind: 'run_merge', status: 'success' }),
          makeJob({ kind: 'run_update_db', status: 'running' }),
        ],
      ),
    )

    const wrapper = await mountUpdates(fetchMock)

    expect(wrapper.find('[data-test="updates-tab-stores"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="updates-tab-local"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="operation-panel"]').exists()).toBe(true)

    const row = wrapper.find('[data-test="updates-store-1"]')
    expect(row.exists()).toBe(true)
    expect(row.text()).toContain('Магазин №5')
    expect(row.text()).toContain('✓') // файл из матрицы
    expect(row.text()).toContain('успех') // merge
    expect(row.text()).toContain('выполняется') // update
    expect(wrapper.text()).toContain('Журнал операций')
    wrapper.unmount()
  })

  it('вкладка «Локальные базы»: плоская таблица базы, панель операций и реквизиты (Д-20/Д-26)', async () => {
    fetchMock.mockImplementation(
      apiMock([], [], [], [], [], [
        makeLocalBase(),
        makeLocalBase({ id: 3, name: 'Торговля', database_name: 'trade', group: 'Продажи' }),
        makeLocalBase({ id: 4, name: 'Зарплата', database_name: 'zp', group: 'Финансы' }),
      ]),
    )

    const wrapper = await mountUpdates(fetchMock)
    await wrapper.find('[data-test="updates-tab-local"]').trigger('click')
    await flushPromises()

    // Все базы в одной плоской таблице (как вкладка «Магазины»), без секций групп
    expect(wrapper.findAll('[data-test^="lb-row-"]')).toHaveLength(3)
    expect(wrapper.find('[data-test="local-operation-panel"]').exists()).toBe(true)

    // Реквизиты базы: кластер/БД, путь, расширение
    const row = wrapper.find('[data-test="lb-row-2"]')
    expect(row.text()).toContain('buh')
    expect(row.text()).toContain('C:\\1C\\buh')
    expect(row.text()).toContain('БГ_Бухгалтерия')
    wrapper.unmount()
  })

  it('вкладка «Локальные базы»: кнопка «Добавить базу» открывает форму', async () => {
    fetchMock.mockImplementation(apiMock([], [], [], [], [], []))

    const wrapper = await mountUpdates(fetchMock)
    await wrapper.find('[data-test="updates-tab-local"]').trigger('click')

    await wrapper.find('[data-test="updates-local-add"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="local-base-dialog"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="lb-name"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="lb-group"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('вкладка «Локальные базы»: статус обновления по базе из run_update_db_local (Д-24)', async () => {
    fetchMock.mockImplementation(
      apiMock(
        [],
        [],
        [],
        [],
        [],
        [makeLocalBase()],
        [
          makeJob({
            id: 9, store_id: null, store_name: null, local_base_id: 2, local_base_name: 'Бухгалтерия',
            kind: 'run_update_db_local', status: 'success',
            created_at: '2026-10-13T10:00:00Z', finished_at: '2026-10-13T10:05:00Z',
          }),
        ],
      ),
    )

    const wrapper = await mountUpdates(fetchMock)
    await wrapper.find('[data-test="updates-tab-local"]').trigger('click')
    await flushPromises()

    // Статус последнего обновления показывается в строке базы и в журнале
    const status = wrapper.find('[data-test="lb-status-2"]')
    expect(status.exists()).toBe(true)
    expect(status.text()).toContain('успех')
    expect(wrapper.text()).toContain('Журнал операций по локальным базам')
    expect(wrapper.text()).toContain('Бухгалтерия')
    wrapper.unmount()
  })

  it('вкладка «Локальные базы»: «Обновить базу» открывает диалог (Д-24)', async () => {
    fetchMock.mockImplementation(apiMock([], [], [], [], [], [makeLocalBase()]))

    const wrapper = await mountUpdates(fetchMock)
    await wrapper.find('[data-test="updates-tab-local"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-test="lb-update-2"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="local-base-update-dialog"]').exists()).toBe(true)
    // В режиме «Обновить базу» поле имени файла не нужно (файл нужен для слияния/проверки)
    expect(wrapper.find('[data-test="lbu-file"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="lbu-cf"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="lbu-cfe"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('вкладка «Локальные базы»: запуск шлёт POST .../run-update (Д-24)', async () => {
    const runUpdateBody: unknown[] = []
    fetchMock.mockImplementation(
      apiMock([], [], [], [], [], [makeLocalBase()], [], [], [], (body) => runUpdateBody.push(body)),
    )

    const wrapper = await mountUpdates(fetchMock)
    await wrapper.find('[data-test="updates-tab-local"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-test="lb-update-2"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-test="lbu-cfe"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-test="lbu-extension"]').setValue('БГ_Бухгалтерия')
    await wrapper.find('[data-test="lbu-run"]').trigger('click')
    await flushPromises()

    expect(runUpdateBody).toHaveLength(1)
    // Имя файла обновление БД не требует — в запрос оно не попадает
    expect(JSON.parse(String(runUpdateBody[0]))).toMatchObject({
      is_extension: true,
      extension_name: 'БГ_Бухгалтерия',
      is_fast_update: false,
    })
    wrapper.unmount()
  })

  it('вкладка «Локальные базы»: массовое обновление выбранных шлёт POST .../bulk/update (Д-25)', async () => {
    const bulkBodies: unknown[] = []
    fetchMock.mockImplementation(
      apiMock(
        [],
        [],
        [],
        [],
        [],
        [makeLocalBase(), makeLocalBase({ id: 3, name: 'Торговля', database_name: 'trade', group: 'Продажи' })],
        [],
        [],
        [],
        undefined,
        (body) => bulkBodies.push(body),
      ),
    )

    const wrapper = await mountUpdates(fetchMock)
    await wrapper.find('[data-test="updates-tab-local"]').trigger('click')
    await flushPromises()

    // Выбрать две базы и открыть массовый диалог
    await wrapper.find('[data-test="lb-select-2"]').setValue(true)
    await wrapper.find('[data-test="lb-select-3"]').setValue(true)
    await flushPromises()
    await wrapper.find('[data-test="updates-local-update-selected"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="local-base-update-dialog"]').exists()).toBe(true)

    await wrapper.find('[data-test="lbu-run"]').trigger('click')
    await flushPromises()

    expect(bulkBodies).toHaveLength(1)
    expect(JSON.parse(String(bulkBodies[0]))).toMatchObject({
      local_base_ids: [2, 3],
      kind: 'run_update_db_local',
      is_fast_update: false,
      is_extension: false,
    })
    wrapper.unmount()
  })

  it('вкладка «Локальные базы»: «Проверить файлы» шлёт POST .../check-files (Д-26)', async () => {
    const checkBodies: unknown[] = []
    fetchMock.mockImplementation(
      apiMock([], [], [], [], [], [makeLocalBase()], [], [], [], undefined, undefined, (body) =>
        checkBodies.push(body),
      ),
    )

    const wrapper = await mountUpdates(fetchMock)
    await wrapper.find('[data-test="updates-tab-local"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-test="lb-check-2"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="local-base-update-dialog"]').exists()).toBe(true)
    await wrapper.find('[data-test="lbu-file"]').setValue('update')
    await wrapper.find('[data-test="lbu-run"]').trigger('click')
    await flushPromises()

    expect(checkBodies).toHaveLength(1)
    expect(JSON.parse(String(checkBodies[0]))).toMatchObject({ file_name: 'update' })
    wrapper.unmount()
  })

  it('вкладка «Локальные базы»: «Слияние…» открывает диалог и шлёт POST .../run-merge (Д-26)', async () => {
    const mergeBodies: unknown[] = []
    fetchMock.mockImplementation(
      apiMock([], [], [], [], [], [makeLocalBase()], [], [], [], undefined, undefined, undefined, (body) =>
        mergeBodies.push(body),
      ),
    )

    const wrapper = await mountUpdates(fetchMock)
    await wrapper.find('[data-test="updates-tab-local"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-test="lb-merge-2"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="local-base-update-dialog"]').exists()).toBe(true)
    await wrapper.find('[data-test="lbu-file"]').setValue('update')
    await wrapper.find('[data-test="lbu-run"]').trigger('click')
    await flushPromises()

    expect(mergeBodies).toHaveLength(1)
    expect(JSON.parse(String(mergeBodies[0]))).toMatchObject({
      file_name: 'update',
      is_extension: false,
    })
    wrapper.unmount()
  })

  it('вкладка «Локальные базы»: массовое «Проверить файлы» шлёт bulk/update с kind=check (Д-26)', async () => {
    const bulkBodies: unknown[] = []
    fetchMock.mockImplementation(
      apiMock(
        [],
        [],
        [],
        [],
        [],
        [makeLocalBase(), makeLocalBase({ id: 3, name: 'Торговля', database_name: 'trade', group: 'Продажи' })],
        [],
        [],
        [],
        undefined,
        (body) => bulkBodies.push(body),
      ),
    )

    const wrapper = await mountUpdates(fetchMock)
    await wrapper.find('[data-test="updates-tab-local"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-test="lb-select-2"]').setValue(true)
    await wrapper.find('[data-test="lb-select-3"]').setValue(true)
    await flushPromises()

    await wrapper.find('[data-test="updates-local-check-selected"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="local-base-update-dialog"]').exists()).toBe(true)
    await wrapper.find('[data-test="lbu-file"]').setValue('update')
    await wrapper.find('[data-test="lbu-run"]').trigger('click')
    await flushPromises()

    expect(bulkBodies).toHaveLength(1)
    expect(JSON.parse(String(bulkBodies[0]))).toMatchObject({
      local_base_ids: [2, 3],
      kind: 'check_files_local',
      file_name: 'update',
    })
    wrapper.unmount()
  })

  it('вкладка «Локальные базы»: массовое «Слияние…» шлёт bulk/update с kind=run_merge_local (Д-26)', async () => {
    const bulkBodies: unknown[] = []
    fetchMock.mockImplementation(
      apiMock(
        [],
        [],
        [],
        [],
        [],
        [makeLocalBase(), makeLocalBase({ id: 3, name: 'Торговля', database_name: 'trade', group: 'Продажи' })],
        [],
        [],
        [],
        undefined,
        (body) => bulkBodies.push(body),
      ),
    )

    const wrapper = await mountUpdates(fetchMock)
    await wrapper.find('[data-test="updates-tab-local"]').trigger('click')
    await flushPromises()

    await wrapper.find('[data-test="lb-select-2"]').setValue(true)
    await wrapper.find('[data-test="lb-select-3"]').setValue(true)
    await flushPromises()

    await wrapper.find('[data-test="updates-local-merge-selected"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="local-base-update-dialog"]').exists()).toBe(true)
    await wrapper.find('[data-test="lbu-file"]').setValue('update')
    await wrapper.find('[data-test="lbu-run"]').trigger('click')
    await flushPromises()

    expect(bulkBodies).toHaveLength(1)
    expect(JSON.parse(String(bulkBodies[0]))).toMatchObject({
      local_base_ids: [2, 3],
      kind: 'run_merge_local',
      file_name: 'update',
    })
    wrapper.unmount()
  })

  it('вкладка «Локальные базы»: панель операций «Обновить» шлёт bulk/update по всем базам (Д-26)', async () => {
    const bulkBodies: unknown[] = []
    fetchMock.mockImplementation(
      apiMock(
        [],
        [],
        [],
        [],
        [],
        [makeLocalBase(), makeLocalBase({ id: 3, name: 'Торговля', database_name: 'trade' })],
        [],
        [],
        [],
        undefined,
        (body) => bulkBodies.push(body),
      ),
    )

    const wrapper = await mountUpdates(fetchMock)
    await wrapper.find('[data-test="updates-tab-local"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="local-operation-panel"]').exists()).toBe(true)
    await wrapper.find('[data-test="local-run-update"]').trigger('click')
    await flushPromises()

    expect(bulkBodies).toHaveLength(1)
    expect(JSON.parse(String(bulkBodies[0]))).toMatchObject({
      kind: 'run_update_db_local',
      local_base_ids: [2, 3],
      is_fast_update: false,
      is_extension: false,
    })
    wrapper.unmount()
  })
})
