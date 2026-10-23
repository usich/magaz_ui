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

function makeJob(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 7, store_id: null, store_name: null, local_base_id: 2, local_base_name: 'Бухгалтерия',
    kind: 'run_update_db_local', status: 'success', params: {}, attempts: 0, last_error: null,
    log_files: [], created_by: 'admin', created_at: '2026-10-13T10:00:00Z',
    started_at: '2026-10-13T10:00:01Z', finished_at: '2026-10-13T10:05:00Z',
    last_message: null, excerpt_preview: null, ...overrides,
  }
}

/** Маршрутизатор fetch: магазинные/локальные базы, журнал локальных обновлений. */
function apiMock(
  localBases: unknown[],
  localJobs: unknown[] = [],
  onBulkUpdate?: (body: unknown) => void,
  onCheckFiles?: (body: unknown) => void,
  onRunMerge?: (body: unknown) => void,
) {
  return async (url: string, init: RequestInit = {}): Promise<Response> => {
    if (init.method === 'POST' && url.includes('/check-files')) {
      onCheckFiles?.(init.body)
      return jsonResponse({
        message: 'Проверка файлов базы поставлена в очередь',
        job: { id: 12, local_base_id: 2, local_base_name: 'Бухгалтерия', kind: 'check_files_local', status: 'queued', created_at: '2026-10-13T10:00:00Z' },
      })
    }
    if (init.method === 'POST' && url.includes('/run-merge')) {
      onRunMerge?.(init.body)
      return jsonResponse({
        message: 'Слияние базы поставлено в очередь',
        job: { id: 13, local_base_id: 2, local_base_name: 'Бухгалтерия', kind: 'run_merge_local', status: 'queued', created_at: '2026-10-13T10:00:00Z' },
      })
    }
    if (init.method === 'POST' && url.includes('/bulk/update')) {
      onBulkUpdate?.(init.body)
      return jsonResponse({
        message: 'Задачи поставлены: 2 из 2 баз',
        total: 2,
        jobs: [
          { local_base_id: 2, local_base_name: 'Бухгалтерия', job_id: 10, status: 'queued', detail: null },
          { local_base_id: 3, local_base_name: 'Торговля', job_id: 11, status: 'queued', detail: null },
        ],
      })
    }
    if (url.includes('/admin/local-bases')) return jsonResponse(localBases)
    if (url.includes('/admin/jobs')) {
      if (url.includes('kind=run_update_db_local'))
        return jsonResponse({ items: localJobs, counts: {}, total: 0, active: 0 })
      return jsonResponse({ items: [], counts: {}, total: 0, active: 0 })
    }
    return jsonResponse([])
  }
}
const stubs = {
  Button: { props: ['label', 'disabled'], template: '<button :disabled="disabled">{{ label }}</button>' },
  Tag: { props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  Message: { template: '<div class="stub-msg"><slot /></div>' },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /><slot name="footer" /></div>' },
  Checkbox: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input type="checkbox" :checked="modelValue" @change="$emit(\'update:modelValue\', $event.target.checked)" />',
  },
  RadioButton: { template: '<span />' },
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
    fetchMock.mockImplementation(
      apiMock([
        makeBase(),
        makeBase({ id: 3, name: 'Зарплата', database_name: 'zp', group: 'Финансы', backup_status: 'missing' }),
        makeBase({ id: 4, name: 'Торговля', database_name: 'trade', group: 'Продажи' }),
      ]),
    )

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
    fetchMock.mockImplementation(apiMock([]))
    const wrapper = await mountView(fetchMock)

    await wrapper.find('[data-test="lb-add"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="local-base-dialog"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('показывает статус последнего обновления базы из run_update_db_local (Д-24)', async () => {
    fetchMock.mockImplementation(apiMock([makeBase()], [makeJob()]))

    const wrapper = await mountView(fetchMock)

    const status = wrapper.find('[data-test="lb-status-2"]')
    expect(status.exists()).toBe(true)
    expect(status.text()).toContain('успех')
    wrapper.unmount()
  })

  it('кнопка «Обновить базу» открывает диалог обновления (Д-24)', async () => {
    fetchMock.mockImplementation(apiMock([makeBase()]))

    const wrapper = await mountView(fetchMock)

    await wrapper.find('[data-test="lb-update-2"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="local-base-update-dialog"]').exists()).toBe(true)
    // В режиме «Обновить базу» поле имени файла не нужно (файл нужен для слияния/проверки)
    expect(wrapper.find('[data-test="lbu-file"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="lbu-cf"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('кнопка «Обновить выбранные» шлёт POST .../bulk/update с выбранными базами (Д-25)', async () => {
    const bulkBodies: unknown[] = []
    fetchMock.mockImplementation(
      apiMock(
        [
          makeBase(),
          makeBase({ id: 3, name: 'Торговля', database_name: 'trade', group: 'Продажи' }),
        ],
        [],
        (body) => bulkBodies.push(body),
      ),
    )

    const wrapper = await mountView(fetchMock)

    await wrapper.find('[data-test="lb-select-2"]').setValue(true)
    await wrapper.find('[data-test="lb-select-3"]').setValue(true)
    await flushPromises()

    await wrapper.find('[data-test="lb-update-selected"]').trigger('click')
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

  it('кнопка «Проверить файлы» шлёт POST .../check-files (Д-26)', async () => {
    const checkBodies: unknown[] = []
    fetchMock.mockImplementation(apiMock([makeBase()], [], undefined, (body) => checkBodies.push(body)))

    const wrapper = await mountView(fetchMock)

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

  it('кнопка «Слияние…» открывает диалог и шлёт POST .../run-merge (Д-26)', async () => {
    const mergeBodies: unknown[] = []
    fetchMock.mockImplementation(
      apiMock([makeBase()], [], undefined, undefined, (body) => mergeBodies.push(body)),
    )

    const wrapper = await mountView(fetchMock)

    await wrapper.find('[data-test="lb-merge-2"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="local-base-update-dialog"]').exists()).toBe(true)
    await wrapper.find('[data-test="lbu-file"]').setValue('update')
    await wrapper.find('[data-test="lbu-run"]').trigger('click')
    await flushPromises()

    expect(mergeBodies).toHaveLength(1)
    expect(JSON.parse(String(mergeBodies[0]))).toMatchObject({ file_name: 'update' })
    wrapper.unmount()
  })

  it('массовое «Проверить файлы» шлёт bulk/update с kind=check_files_local (Д-26)', async () => {
    const bulkBodies: unknown[] = []
    fetchMock.mockImplementation(
      apiMock(
        [makeBase(), makeBase({ id: 3, name: 'Торговля', database_name: 'trade', group: 'Продажи' })],
        [],
        (body) => bulkBodies.push(body),
      ),
    )

    const wrapper = await mountView(fetchMock)

    await wrapper.find('[data-test="lb-select-2"]').setValue(true)
    await wrapper.find('[data-test="lb-select-3"]').setValue(true)
    await flushPromises()

    await wrapper.find('[data-test="lb-check-selected"]').trigger('click')
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
})
