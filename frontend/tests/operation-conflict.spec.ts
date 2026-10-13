/**
 * Конфликт запуска R12 (§11.4, §5.2.2, П-5) в панели операций.
 *
 * Сервер отвечает `409`, когда запуск нарушает запрет «одна серверная ИБ — одно
 * обновление». Для админа это не «поломка», а запрет с объяснением, поэтому
 * панель показывает его отдельным жёлтым блоком, а не красной ошибкой.
 *
 * PrimeVue подменяется заглушками: проверяется логика панели, а не оформление.
 */

import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const toastAdd = vi.fn()
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }))

import OperationPanel from '@/components/OperationPanel.vue'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** Заглушки PrimeVue: контракт v-model не нужен — панель проверяем по данным. */
const stubs = {
  Button: {
    props: ['label'],
    emits: ['click'],
    template: '<button @click="$emit(\'click\')"><slot /></button>',
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :value="modelValue" />',
  },
  Checkbox: { props: ['modelValue'], template: '<label><slot /></label>' },
  Message: {
    props: ['severity'],
    template: '<div :data-severity="severity"><slot /></div>',
  },
}

function mountPanel() {
  return mount(OperationPanel, {
    props: { storeIds: [1] },
    global: { stubs },
  })
}

describe('конфликт запуска R12 (§11.4)', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    toastAdd.mockClear()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('409 показывается жёлтым блоком с объяснением, кого исключить', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse(
        {
          detail:
            'Запуск отменён: нарушен запрет одновременного обновления одной серверной ИБ (R12), ' +
            'задачи не созданы. 1) магазины «Магазин №5», «Магазин №6» работают с одной ' +
            'серверной ИБ «srv-1c\\trade»: одновременное обновление запрещено — исключите один ' +
            'из магазинов из запуска и повторите.',
        },
        409,
      ),
    )
    const panel = mountPanel()

    await panel.find('[data-test="run-merge"]').trigger('click')
    await flushPromises()

    const conflict = panel.find('[data-test="operation-conflict"]')
    expect(conflict.exists()).toBe(true)
    expect(conflict.attributes('data-severity')).toBe('warn')
    expect(conflict.text()).toContain('не обновляется дважды')
    expect(conflict.text()).toContain('Магазин №6')
    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'warn', summary: 'Конфликт запуска (R12)' }),
    )
  })

  it('успешный запуск блок конфликта не показывает', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse({
        job: {
          id: 7,
          store_id: 1,
          store_name: 'Магазин №5',
          kind: 'run_merge',
          status: 'queued',
        },
        message: 'Задача #7 поставлена в очередь',
        warning: null,
      }),
    )
    const panel = mountPanel()

    await panel.find('[data-test="run-merge"]').trigger('click')
    await flushPromises()

    expect(panel.find('[data-test="operation-conflict"]').exists()).toBe(false)
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'success' }))
  })
})
