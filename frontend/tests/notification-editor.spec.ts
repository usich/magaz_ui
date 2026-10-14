/**
 * Тесты редактора уведомления и показа тела (§11.5, §4.6, Э9).
 *
 * Проверяется то, за что отвечает админка, а не ProseMirror: панель форматов,
 * картинка как черновик (в тело уходит метка `magaz-image:<имя>`, а не адрес),
 * и «Просмотреть» — предпросмотр показывает то же сообщение, что уйдёт на кассу.
 */

import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import type { Editor } from '@tiptap/vue-3'

import NotificationBody from '@/components/NotificationBody.vue'
import RichTextEditor from '@/components/RichTextEditor.vue'
import type { NotificationImageDraft } from '@/stores/notifications'

/** Настоящий PNG 1×1: размер и тип важны — редактор проверяет файл до отправки. */
const PNG_BYTES = Uint8Array.from([
  0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
])

function makeDraft(overrides: Partial<NotificationImageDraft> = {}): NotificationImageDraft {
  return {
    name: 'img-a1b2c3d4.png',
    contentType: 'image/png',
    dataUrl: 'data:image/png;base64,iVBORw0KGgo=',
    sizeBytes: 2048,
    sourceName: 'схема зала.png',
    ...overrides,
  }
}

function lastEmitted(wrapper: VueWrapper, event: string): unknown {
  const events = wrapper.emitted(event)
  return events?.[events.length - 1]?.[0]
}

describe('RichTextEditor (§11.5, Э9)', () => {
  it('показывает панель форматов и поле ввода', async () => {
    const wrapper = mount(RichTextEditor, { props: { modelValue: '<p>Смена</p>' } })
    await flushPromises()

    expect(wrapper.find('[data-test="rte"]').exists()).toBe(true)
    for (const test of [
      'rte-bold',
      'rte-italic',
      'rte-underline',
      'rte-h2',
      'rte-bullet',
      'rte-link',
      'rte-align-center',
      'rte-color',
      'rte-image',
    ]) {
      expect(wrapper.find(`[data-test="${test}"]`).exists(), test).toBe(true)
    }
    expect(wrapper.find('.ProseMirror').text()).toContain('Смена')
  })

  it('жирный текст уходит разметкой, а не текстом — санитайз сервера её пропустит', async () => {
    const wrapper = mount(RichTextEditor, { props: { modelValue: '<p>Смена</p>' } })
    await flushPromises()

    // Формат применяется к выделению: без него ProseMirror запомнил бы «жирный»
    // для следующего ввода и не изменил документ (то есть не отдал бы разметку)
    const editor = (wrapper.vm as unknown as { editor: Editor }).editor
    editor.chain().selectAll().run()

    await wrapper.find('[data-test="rte-bold"]').trigger('click')
    await flushPromises()

    expect(String(lastEmitted(wrapper, 'update:modelValue'))).toContain('<strong>')
  })

  it('картинка становится черновиком, а в теле остаётся метка magaz-image (§7.4)', async () => {
    const wrapper = mount(RichTextEditor, { props: { modelValue: '' } })
    await flushPromises()

    const input = wrapper.find('[data-test="rte-file"]')
    const file = new File([PNG_BYTES], 'shema.png', { type: 'image/png' })
    Object.defineProperty(input.element, 'files', { value: [file] })
    await input.trigger('change')
    await vi.waitFor(() => expect(wrapper.emitted('update:drafts')).toBeTruthy())

    const drafts = lastEmitted(wrapper, 'update:drafts') as NotificationImageDraft[]
    expect(drafts).toHaveLength(1)
    expect(drafts[0].name).toMatch(/^img-[0-9a-f]{8}\.png$/)
    expect(drafts[0].sourceName).toBe('shema.png')
    expect(drafts[0].dataUrl.startsWith('data:image/png;base64,')).toBe(true)
    expect(drafts[0].sizeBytes).toBe(PNG_BYTES.length)

    const body = String(lastEmitted(wrapper, 'update:modelValue'))
    expect(body).toContain(`magaz-image:${drafts[0].name}`)
    expect(body).not.toContain('data:image/png')
  })

  it('слишком большая картинка объясняет отказ и не попадает в тело', async () => {
    const wrapper = mount(RichTextEditor, { props: { modelValue: '' } })
    await flushPromises()

    const input = wrapper.find('[data-test="rte-file"]')
    const big = new File([new Uint8Array(3 * 1024 * 1024)], 'foto.png', { type: 'image/png' })
    Object.defineProperty(input.element, 'files', { value: [big] })
    await input.trigger('change')
    await flushPromises()

    expect(wrapper.find('[data-test="rte-error"]').text()).toContain('МБ')
    expect(wrapper.emitted('update:drafts')).toBeFalsy()
  })

  it('приложенные картинки видны списком, и «убрать» удаляет и черновик (§11.5)', async () => {
    const draft = makeDraft()
    const wrapper = mount(RichTextEditor, {
      props: {
        modelValue: `<p>Смотри</p><img src="magaz-image:${draft.name}">`,
        drafts: [draft],
      },
    })
    await flushPromises()

    expect(wrapper.find('[data-test="rte-images"]').text()).toContain('схема зала.png')
    await wrapper.find('[data-test="rte-image-remove"]').trigger('click')
    await flushPromises()

    expect(lastEmitted(wrapper, 'update:drafts')).toEqual([])
    expect(String(lastEmitted(wrapper, 'update:modelValue'))).not.toContain('magaz-image')
  })
})

describe('NotificationBody (§4.6, §11.5, Э9)', () => {
  it('метку картинки превращает в адрес эндпоинта', async () => {
    const wrapper = mount(NotificationBody, {
      props: {
        body: '<p>Схема</p><img src="magaz-image:shema.png">',
        bodyFormat: 'html',
        notificationId: 7,
      },
    })
    await flushPromises()

    const image = wrapper.find('img')
    expect(image.attributes('src')).toBe('/api/v1/admin/notifications/7/images/shema.png')
    expect(image.attributes('alt')).toBe('shema.png')
    expect(wrapper.text()).toContain('Схема')
  })

  it('до сохранения показывает картинку-черновик из редактора', async () => {
    const draft = makeDraft()
    const wrapper = mount(NotificationBody, {
      props: {
        body: `<img src="magaz-image:${draft.name}">`,
        bodyFormat: 'html',
        drafts: [draft],
      },
    })
    await flushPromises()

    expect(wrapper.find('img').attributes('src')).toBe(draft.dataUrl)
  })

  it('метка без картинки не рисуется «сломанной» иконкой', async () => {
    const wrapper = mount(NotificationBody, {
      props: { body: '<p>Текст</p><img src="magaz-image:net.png">', bodyFormat: 'html' },
    })
    await flushPromises()

    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.text()).toContain('Текст')
  })

  it('простое тело экранируется: `<` остаётся текстом, перевод строки — переносом', async () => {
    const wrapper = mount(NotificationBody, {
      props: { body: 'Смена < 22:00\nЗакройте кассу', bodyFormat: 'plain' },
    })
    await flushPromises()

    expect(wrapper.find('br').exists()).toBe(true)
    expect(wrapper.text()).toContain('Смена < 22:00')
    expect(wrapper.html()).toContain('&lt;')
  })
})
