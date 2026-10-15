<script setup lang="ts">
/**
 * Редактор тела уведомления (§11.5, Э9): разметка и картинки.
 *
 * TipTap (ProseMirror) даёт то, чего не было у простого `<textarea>`: жирный,
 * списки, ссылки, цвет и картинки. Правила сервера важнее возможностей редактора,
 * поэтому панель ограничена тем, что переживёт санитайз (`shared.richtext`):
 * заголовки h2/h3, выравнивание и цвет — безопасными свойствами стиля.
 *
 * Картинки не уезжают на сервер по одной: файл остаётся черновиком
 * (`NotificationImageDraft`), а в теле появляется метка `magaz-image:<имя>`.
 * Черновики уходят вместе с уведомлением (§7.4) — так касса никогда не получит
 * тело, в котором метка есть, а файла нет.
 */
import { computed, ref, watch } from 'vue'
import { mergeAttributes } from '@tiptap/core'
import Image from '@tiptap/extension-image'
import StarterKit from '@tiptap/starter-kit'
import TextAlign from '@tiptap/extension-text-align'
import { BackgroundColor, Color, TextStyle } from '@tiptap/extension-text-style'
import { EditorContent, useEditor } from '@tiptap/vue-3'

import {
  IMAGE_CONTENT_TYPES,
  IMAGE_MAX_BYTES,
  IMAGES_MAX_PER_NOTIFICATION,
  IMAGE_SRC_PREFIX,
  imageFileError,
  imageNameFromSrc,
  makeImageName,
  notificationImageUrl,
  sizeMb,
  type NotificationImageDraft,
} from '@/stores/notifications'

const props = withDefaults(
  defineProps<{
    /** Тело на HTML: сервер примет его только с `body_format: 'html'` (§4.6). */
    modelValue: string
    /** Черновики картинок: `data:`-URL по имени (редактор их создаёт и хранит). */
    drafts?: NotificationImageDraft[]
    notificationId?: number | null
    disabled?: boolean
  }>(),
  { drafts: () => [], notificationId: null, disabled: false },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  'update:drafts': [value: NotificationImageDraft[]]
}>()

/** Цвета текста и подсветки: те же, что пройдут санитайз (`color`, `background-color`). */
const TEXT_COLORS = [
  { value: '', label: 'цвет текста' },
  { value: '#b91c1c', label: 'красный' },
  { value: '#b45309', label: 'оранжевый' },
  { value: '#15803d', label: 'зелёный' },
  { value: '#7e22ce', label: 'фиолетовый' },
]
const HIGHLIGHTS = [
  { value: '', label: 'подсветка' },
  { value: '#fef08a', label: 'жёлтый' },
  { value: '#bbf7d0', label: 'салатовый' },
  { value: '#bfdbfe', label: 'голубой' },
]

const errorMessage = ref('')
/** Счётчик транзакций: выделение и формат меняются без правки текста (§11.5). */
const editorTick = ref(0)
const fileInput = ref<HTMLInputElement | null>(null)
const drafts = computed(() => props.drafts)

/** Адрес картинки для редактора: черновик до сохранения, эндпоинт после (§7.4). */
function resolveImage(name: string): string {
  const draft = drafts.value.find((item) => item.name === name)
  return draft?.dataUrl ?? notificationImageUrl(props.notificationId, name)
}

/** Расширение по типу картинки: имя файла должно оставаться понятным (§4.6). */
const IMAGE_EXTENSIONS: Record<string, string> = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/gif': 'gif',
  'image/webp': 'webp',
}

/** Ширина картинки в атрибуте: `320px`, `50%` или пусто — «как есть» (§4.6). */
const IMAGE_WIDTH_PATTERN = /^\d{1,4}(px|%)$/

/** Шаг кнопок «уже/шире» — доля ширины области редактирования. */
const IMAGE_WIDTH_STEP_SHARE = 0.1
/** Ниже этого картинку не уменьшаем: подпись и крестик должны помещаться. */
const IMAGE_WIDTH_MIN = 60

/**
 * Картинки, которые нужно дорисовать: черновик приходит уже после вставки узла.
 *
 * `stageFiles` сначала вставляет узел `magaz-image:<имя>` и только потом отдаёт
 * черновик наружу, а `drafts` возвращаются пропом на следующем тике. Без этого
 * реестра узел остался бы с пустым `src` — «картинка не вставилась» (§11.5, Э9).
 */
const imageRenderers = new Set<() => void>()

/** Ширина из атрибута или стиля: всё, что не число с `px`/`%`, — «как есть». */
function normalizeImageWidth(value: unknown): string {
  const text = String(value ?? '').trim().toLowerCase()
  return IMAGE_WIDTH_PATTERN.test(text) ? text : ''
}

/**
 * Узел картинки: в документе живёт метка `magaz-image:<имя>`, а на экране —
 * сама картинка. Иначе админ видел бы «пропавшее изображение»: файла на сервере
 * ещё нет, уведомление не создано.
 *
 * Размер картинки живёт в атрибуте `width` и уезжает **стилем** (`style="width:
 * 50%"`): сервер пропускает `width`/`max-width` в стиле (§4.6), а сам атрибут
 * `width` HTML оставил бы картинку с «зашитым» размером.
 */
const MagazImage = Image.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      width: {
        default: '',
        parseHTML: (element: HTMLElement) =>
          normalizeImageWidth(element.style.width) || normalizeImageWidth(element.getAttribute('width')),
        // Атрибут отдаём как `width` — узел сам превратит его в `style` (см. renderHTML)
        renderHTML: (attributes: { width?: string }) =>
          attributes.width ? { width: attributes.width } : {},
      },
    }
  },
  renderHTML({ HTMLAttributes }) {
    const { width, ...rest } = HTMLAttributes as { width?: string }
    const style = width ? `width: ${width}` : ''
    return ['img', mergeAttributes(this.options.HTMLAttributes, rest, style ? { style } : {})]
  },
  addNodeView() {
    return ({ node, getPos, editor }) => {
      const wrapper = document.createElement('span')
      wrapper.className = 'magaz-image-node'
      const frame = document.createElement('span')
      frame.className = 'magaz-image-frame'
      const picture = document.createElement('img')
      const remove = document.createElement('button')
      remove.type = 'button'
      remove.className = 'magaz-image-remove'
      remove.title = 'Убрать картинку'
      remove.textContent = '✕'
      const resize = document.createElement('span')
      resize.className = 'magaz-image-resize'
      resize.title = 'Потяните, чтобы изменить размер'
      resize.textContent = '⇲'

      let current = node

      /** Позиция узла в документе: `null` — узел уже удалён, менять нечего. */
      const position = (): number | null => {
        const value = typeof getPos === 'function' ? getPos() : null
        return typeof value === 'number' ? value : null
      }

      /** Проставить ширину (`''` — «как есть»): узел перерисуется через `update`. */
      const setWidth = (width: string): void => {
        const at = position()
        if (at === null) return
        editor.view.dispatch(editor.state.tr.setNodeMarkup(at, undefined, { ...current.attrs, width }))
      }

      const render = (): void => {
        const name = imageNameFromSrc(current.attrs.src as string)
        picture.alt = String(current.attrs.alt ?? name ?? 'картинка')
        picture.style.width = normalizeImageWidth(current.attrs.width)
        const src = name ? resolveImage(name) : ''
        if (src) picture.src = src
        else picture.removeAttribute('src')
      }

      // Черновик приходит после вставки узла: подписываемся на его появление,
      // иначе картинка осталась бы без `src` — «вставил, а её не видно» (Э9)
      imageRenderers.add(render)
      render()
      remove.addEventListener('click', () => {
        const at = position()
        if (at === null) return
        editor.chain().focus().deleteRange({ from: at, to: at + current.nodeSize }).run()
      })

      /**
       * Ручка размера: тянем вправо-влево — ширина считается от ширины области
       * редактирования, так админ видит тот же масштаб, что и кассир (§7.4).
       */
      resize.addEventListener('mousedown', (event: MouseEvent) => {
        event.preventDefault()
        event.stopPropagation()
        const startX = event.clientX
        const startWidth = picture.getBoundingClientRect().width || editor.view.dom.clientWidth || 1
        const onMove = (move: MouseEvent): void => {
          const width = Math.max(IMAGE_WIDTH_MIN, Math.round(startWidth + (move.clientX - startX)))
          setWidth(`${width}px`)
        }
        const onUp = (): void => {
          document.removeEventListener('mousemove', onMove)
          document.removeEventListener('mouseup', onUp)
        }
        document.addEventListener('mousemove', onMove)
        document.addEventListener('mouseup', onUp)
      })

      frame.append(picture, remove, resize)
      wrapper.append(frame)
      return {
        dom: wrapper,
        update: (updated) => {
          if (updated.type.name !== current.type.name) return false
          current = updated
          render()
          return true
        },
        destroy: () => {
          imageRenderers.delete(render)
        },
      }
    }
  },
}).configure({ inline: false })

const editor = useEditor({
  content: props.modelValue,
  editable: !props.disabled,
  extensions: [
    StarterKit.configure({
      heading: { levels: [2, 3] },
      link: {
        openOnClick: false,
        autolink: true,
        protocols: ['http', 'https', 'mailto'],
      },
    }),
    TextStyle,
    Color,
    BackgroundColor,
    TextAlign.configure({ types: ['heading', 'paragraph'] }),
    MagazImage,
  ],
  editorProps: {
    attributes: {
      class: 'magaz-richtext min-h-[9rem] rounded-b-lg bg-white px-3 py-2 outline-none',
      'data-test': 'rte-editor',
    },
    handlePaste: (_view, event) => insertFromData(event.clipboardData),
    handleDrop: (_view, event) => insertFromData(event.dataTransfer),
  },
  onUpdate: ({ editor: instance }) => emit('update:modelValue', instance.getHTML()),
  onTransaction: () => {
    editorTick.value += 1
  },
})

/** Разметка синхронна и «снаружи»: форму открывают заново — тело должно сброситься. */
watch(
  () => props.modelValue,
  (value) => {
    const instance = editor.value
    if (instance && value !== instance.getHTML()) {
      instance.commands.setContent(value, { emitUpdate: false })
    }
  },
)

/**
 * Картинки дорисовываются, когда приходит черновик: узел `magaz-image:<имя>`
 * появляется в теле раньше, чем родитель отдаёт `drafts` обратно пропом, и без
 * этого перерисовывания в редакторе было бы пустое место вместо картинки (Э9).
 */
watch(
  () => props.drafts,
  () => {
    imageRenderers.forEach((render) => render())
  },
  { deep: true },
)

/**
 * Разложить файлы по черновикам и поставить в тело по узлу на каждый.
 *
 * Картинка превращается в `data:`-URL: так её видно в редакторе и предпросмотре,
 * а на сервер она уедет тем же телом (`data_base64`) при создании уведомления.
 */
async function stageFiles(files: File[]): Promise<void> {
  if (!files.length) return
  const staged = [...drafts.value]
  const added: NotificationImageDraft[] = []
  for (const file of files) {
    if (staged.length >= IMAGES_MAX_PER_NOTIFICATION) {
      errorMessage.value =
        `К одному уведомлению можно приложить не больше ` +
        `${IMAGES_MAX_PER_NOTIFICATION} картинок`
      break
    }
    // Тип и размер — по метаданным файла: 3-МБ картинку незачем превращать в base64
    const quickReason = imageFileError(file)
    if (quickReason) {
      errorMessage.value = `${file.name || 'картинка'}: ${quickReason}`
      continue
    }
    const dataUrl = await readAsDataUrl(file)
    const contentType = imageContentType(file, dataUrl)
    if (!contentType) {
      errorMessage.value = `${file.name || 'картинка'}: это не PNG, JPEG, GIF или WebP`
      continue
    }
    if (file.size === 0 && dataUrlSize(dataUrl) > IMAGE_MAX_BYTES) {
      errorMessage.value = `${file.name || 'картинка'}: картинка больше ${sizeMb(IMAGE_MAX_BYTES)}`
      continue
    }
    const draft: NotificationImageDraft = {
      name: makeImageName(file.name),
      contentType,
      dataUrl,
      sizeBytes: file.size || dataUrlSize(dataUrl),
      sourceName: file.name || 'картинка',
    }
    staged.push(draft)
    added.push(draft)
  }
  // Черновики отдаём наружу **до** вставки узлов: узел появляется в теле раньше,
  // чем родитель вернёт `drafts` пропом, и картинку дорисует `imageRenderers` (Э9)
  if (added.length) emit('update:drafts', staged)
  for (const draft of added) {
    editor.value
      ?.chain()
      .focus()
      .setImage({ src: `${IMAGE_SRC_PREFIX}${draft.name}`, alt: draft.sourceName })
      .run()
  }
}

/**
 * Тип картинки: сначала «магические» байты самого файла (`data:image/png;base64,…`),
 * и только потом `file.type`. Так вставка из буфера не теряется, когда браузер
 * отдаёт файл без типа (Э9).
 */
function imageContentType(file: File, dataUrl: string): string {
  const fromData = /^data:([a-z]+\/[a-z0-9.+-]+)[;,]/i.exec(dataUrl)?.[1]?.toLowerCase() ?? ''
  if (IMAGE_CONTENT_TYPES.includes(fromData)) return fromData
  const fromFile = (file.type || '').toLowerCase()
  return IMAGE_CONTENT_TYPES.includes(fromFile) ? fromFile : ''
}

/** Размер картинки по `data:`-URL: base64 — 4 символа на 3 байта (§4.6). */
function dataUrlSize(dataUrl: string): number {
  const separator = dataUrl.indexOf(',')
  if (separator < 0) return 0
  return Math.floor(((dataUrl.length - separator - 1) * 3) / 4)
}

/** Файл-картинка: тип бывает пустым — тогда смотрим на расширение. */
function isImageFile(file: File): boolean {
  return file.type.startsWith('image/') || /\.(png|jpe?g|gif|webp)$/i.test(file.name)
}

/** Картинки из буфера обмена или перетаскивания: тот же путь, что у выбора файла. */
function insertFromData(data: DataTransfer | null): boolean {
  if (!data) return false
  const files = collectImageFiles(data)
  if (files.length) {
    void stageFiles(files)
    return true
  }
  const inline = inlineImageSources(data.getData('text/html'))
  if (inline.length) {
    void stageInlineImages(inline)
    return true
  }
  return false
}

/**
 * Картинки из буфера: браузеры кладут их и в `files`, и в `items` — например,
 * скриншот из «Ножниц» или картинка из Word приходят только во `items` (Э9).
 */
function collectImageFiles(data: DataTransfer): File[] {
  const files: File[] = []
  for (const file of Array.from(data.files ?? [])) {
    if (isImageFile(file)) files.push(file)
  }
  if (!files.length) {
    for (const item of Array.from(data.items ?? [])) {
      if (item.kind !== 'file') continue
      const file = item.getAsFile()
      if (file && isImageFile(file)) files.push(file)
    }
  }
  return files
}

/** `data:`-картинки из скопированной разметки: внешние адреса до кассы не доедут (§4.6). */
function inlineImageSources(html: string): string[] {
  if (!html) return []
  const parsed = new DOMParser().parseFromString(html, 'text/html')
  return [...parsed.querySelectorAll('img')]
    .map((node) => node.getAttribute('src') ?? '')
    .filter((src) => src.toLowerCase().startsWith('data:image/'))
}

/** Разложить картинки из буфера по черновикам — тот же путь, что у файлов (Э9). */
async function stageInlineImages(sources: string[]): Promise<void> {
  const staged = [...drafts.value]
  const added: NotificationImageDraft[] = []
  for (const dataUrl of sources) {
    if (staged.length >= IMAGES_MAX_PER_NOTIFICATION) break
    const contentType = imageContentType(new File([], 'clipboard'), dataUrl)
    if (!contentType) continue
    const extension = IMAGE_EXTENSIONS[contentType] ?? 'png'
    const size = dataUrlSize(dataUrl)
    const reason = imageFileError({ type: contentType, size })
    if (reason) {
      errorMessage.value = `картинка из буфера: ${reason}`
      continue
    }
    const draft: NotificationImageDraft = {
      name: makeImageName(`clipboard.${extension}`),
      contentType,
      dataUrl,
      sizeBytes: size,
      sourceName: `картинка из буфера.${extension}`,
    }
    staged.push(draft)
    added.push(draft)
  }
  if (added.length) emit('update:drafts', staged)
  for (const draft of added) {
    editor.value
      ?.chain()
      .focus()
      .setImage({ src: `${IMAGE_SRC_PREFIX}${draft.name}`, alt: draft.sourceName })
      .run()
  }
}

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result ?? ''))
    reader.onerror = () => resolve('')
    reader.readAsDataURL(file)
  })
}

function pickFiles(): void {
  errorMessage.value = ''
  fileInput.value?.click()
}

function onFileInput(event: Event): void {
  const input = event.target as HTMLInputElement
  void stageFiles(Array.from(input.files ?? []))
  input.value = ''
}

/** Убрать картинку совсем: и узел из тела, и черновик — имя больше не нужно. */
function removeDraft(name: string): void {
  const instance = editor.value
  if (instance) {
    const positions: number[] = []
    instance.state.doc.descendants((node, position) => {
      if (node.type.name === 'image' && imageNameFromSrc(node.attrs.src as string) === name) {
        positions.push(position)
      }
      return true
    })
    for (const position of positions.reverse()) {
      const node = instance.state.doc.nodeAt(position)
      if (node) {
        instance.view.dispatch(instance.state.tr.delete(position, position + node.nodeSize))
      }
    }
    emit('update:modelValue', instance.getHTML())
  }
  emit(
    'update:drafts',
    drafts.value.filter((draft) => draft.name !== name),
  )
}

/** Ссылка: только `http(s)` и `mailto` — остальное сервер всё равно выбросит (§4.6). */
function setLink(): void {
  const instance = editor.value
  if (!instance) return
  const previous = String(instance.getAttributes('link').href ?? '')
  const href = window.prompt('Адрес ссылки (https://… или mailto:…). Пусто — убрать', previous)
  if (href === null) return
  if (!href.trim()) {
    instance.chain().focus().extendMarkRange('link').unsetLink().run()
    return
  }
  instance.chain().focus().extendMarkRange('link').setLink({ href: href.trim() }).run()
}

/** Цвет текста из панели; пустое значение — вернуть обычный цвет (§4.6). */
function setTextColor(value: string): void {
  const instance = editor.value
  if (!instance) return
  if (!value) {
    instance.chain().focus().unsetColor().run()
    return
  }
  instance.chain().focus().setColor(value).run()
}

/** Подсветка: санитайз пропускает `background-color` — «важное» видно и на кассе. */
function setHighlight(value: string): void {
  const instance = editor.value
  if (!instance) return
  if (!value) {
    instance.chain().focus().unsetBackgroundColor().run()
    return
  }
  instance.chain().focus().setBackgroundColor(value).run()
}

// --- Размер картинки (§11.5, Э9) -------------------------------------------

/** Ширина выделенной картинки в пикселях: считаем по тому, что видно админу. */
function selectedImagePixels(): number {
  const picture = document.querySelector<HTMLImageElement>(
    '.magaz-image-node.ProseMirror-selectednode img',
  )
  return picture ? Math.round(picture.getBoundingClientRect().width) : 0
}

/** Записать ширину выделенной картинке; `null` — вернуть натуральный размер. */
function applyImageWidth(width: string | null): void {
  const instance = editor.value
  if (!instance) return
  const { from } = instance.state.selection
  const node = instance.state.doc.nodeAt(from)
  if (!node || node.type.name !== 'image') return
  instance.view.dispatch(
    instance.state.tr.setNodeMarkup(from, undefined, { ...node.attrs, width: width ?? '' }),
  )
  emit('update:modelValue', instance.getHTML())
}

/** «Уже»/«шире» на 10 % ширины области редактирования — шаг заметный, но не грубый. */
function stepImageWidth(direction: 1 | -1): void {
  const instance = editor.value
  const current = selectedImagePixels()
  if (!instance || !current) return
  const available = instance.view.dom.clientWidth || 600
  // Не больше четверти самой картинки: маленькую иначе «перепрыгивало» бы в минимум (Э9)
  const step = Math.max(
    20,
    Math.min(Math.round(available * IMAGE_WIDTH_STEP_SHARE), Math.round(current / 4)),
  )
  applyImageWidth(`${Math.max(IMAGE_WIDTH_MIN, current + direction * step)}px`)
}

/** Доля ширины области редактирования: кнопки «половина» и «во всю ширину». */
function setImageWidthShare(share: number): void {
  const instance = editor.value
  if (!instance) return
  const available = instance.view.dom.clientWidth || 600
  applyImageWidth(`${Math.max(IMAGE_WIDTH_MIN, Math.round(available * share))}px`)
}

/** Состояние кнопок читается из редактора — панель показывает активные форматы (§11.5). */
const can = computed(() => editor.value?.isEditable ?? false)

/** `isActive` принимает и имя формата, и набор атрибутов (например выравнивание). */
function isActive(
  name: string | Record<string, unknown>,
  attrs?: Record<string, unknown>,
): boolean {
  void editorTick.value
  const instance = editor.value
  if (!instance) return false
  return typeof name === 'string' ? instance.isActive(name, attrs) : instance.isActive(name)
}

/** Выделена картинка: только тогда кнопки размера в панели работают (§11.5). */
const imageSelected = computed(() => isActive('image'))

defineExpose({ removeDraft, editor })
</script>


<template>
  <div class="rounded-lg border border-slate-300" data-test="rte">
    <div class="flex flex-wrap items-center gap-1 border-b border-slate-200 bg-slate-50 px-2 py-1">
      <button
        v-for="tool in [
          { test: 'rte-bold', label: 'Ж', title: 'Жирный', active: 'bold', run: () => editor?.chain().focus().toggleBold().run() },
          { test: 'rte-italic', label: 'К', title: 'Курсив', active: 'italic', run: () => editor?.chain().focus().toggleItalic().run() },
          { test: 'rte-underline', label: 'Ч', title: 'Подчёркнутый', active: 'underline', run: () => editor?.chain().focus().toggleUnderline().run() },
          { test: 'rte-strike', label: 'З', title: 'Зачёркнутый', active: 'strike', run: () => editor?.chain().focus().toggleStrike().run() },
        ]"
        :key="tool.test"
        type="button"
        class="rte-btn"
        :class="{ 'rte-btn-active': isActive(tool.active) }"
        :title="tool.title"
        :disabled="!can"
        :data-test="tool.test"
        @click="tool.run()"
      >
        {{ tool.label }}
      </button>

      <span class="rte-sep" />

      <button
        type="button"
        class="rte-btn"
        :class="{ 'rte-btn-active': isActive('heading', { level: 2 }) }"
        :disabled="!can"
        title="Подзаголовок"
        data-test="rte-h2"
        @click="editor?.chain().focus().toggleHeading({ level: 2 }).run()"
      >
        H2
      </button>
      <button
        type="button"
        class="rte-btn"
        :class="{ 'rte-btn-active': isActive('heading', { level: 3 }) }"
        :disabled="!can"
        title="Мелкий подзаголовок"
        data-test="rte-h3"
        @click="editor?.chain().focus().toggleHeading({ level: 3 }).run()"
      >
        H3
      </button>
      <button
        type="button"
        class="rte-btn"
        :class="{ 'rte-btn-active': isActive('bulletList') }"
        :disabled="!can"
        title="Список"
        data-test="rte-bullet"
        @click="editor?.chain().focus().toggleBulletList().run()"
      >
        • список
      </button>
      <button
        type="button"
        class="rte-btn"
        :class="{ 'rte-btn-active': isActive('orderedList') }"
        :disabled="!can"
        title="Нумерованный список"
        data-test="rte-ordered"
        @click="editor?.chain().focus().toggleOrderedList().run()"
      >
        1. список
      </button>
      <button
        type="button"
        class="rte-btn"
        :class="{ 'rte-btn-active': isActive('blockquote') }"
        :disabled="!can"
        title="Цитата"
        data-test="rte-quote"
        @click="editor?.chain().focus().toggleBlockquote().run()"
      >
        ❝
      </button>
      <button
        type="button"
        class="rte-btn"
        :disabled="!can"
        title="Разделитель"
        data-test="rte-hr"
        @click="editor?.chain().focus().setHorizontalRule().run()"
      >
        —
      </button>

      <span class="rte-sep" />

      <button
        type="button"
        class="rte-btn"
        :class="{ 'rte-btn-active': isActive('link') }"
        :disabled="!can"
        title="Вставить ссылку"
        data-test="rte-link"
        @click="setLink"
      >
        ссылка
      </button>
      <button
        type="button"
        class="rte-btn"
        :disabled="!can"
        title="Убрать ссылку"
        data-test="rte-unlink"
        @click="editor?.chain().focus().unsetLink().run()"
      >
        без ссылки
      </button>

      <span class="rte-sep" />

      <button
        v-for="align in ['left', 'center', 'right'] as const"
        :key="align"
        type="button"
        class="rte-btn"
        :class="{ 'rte-btn-active': isActive({ textAlign: align }) }"
        :disabled="!can"
        :title="`Выравнивание: ${align}`"
        :data-test="`rte-align-${align}`"
        @click="editor?.chain().focus().setTextAlign(align).run()"
      >
        {{ align === 'left' ? '⯇' : align === 'center' ? '≡' : '⯈' }}
      </button>

      <span class="rte-sep" />

      <select
        class="rounded border border-slate-300 px-1 py-1 text-xs"
        :disabled="!can"
        data-test="rte-color"
        @change="setTextColor(($event.target as HTMLSelectElement).value)"
      >
        <option v-for="color in TEXT_COLORS" :key="`c-${color.value}`" :value="color.value">
          {{ color.label }}
        </option>
      </select>
      <select
        class="rounded border border-slate-300 px-1 py-1 text-xs"
        :disabled="!can"
        data-test="rte-highlight"
        @change="setHighlight(($event.target as HTMLSelectElement).value)"
      >
        <option v-for="color in HIGHLIGHTS" :key="`h-${color.value}`" :value="color.value">
          {{ color.label }}
        </option>
      </select>

      <span class="rte-sep" />

      <button
        type="button"
        class="rte-btn"
        :disabled="!can"
        title="Приложить картинку"
        data-test="rte-image"
        @click="pickFiles"
      >
        🖼 картинка
      </button>

      <span class="rte-sep" />

      <button
        type="button"
        class="rte-btn"
        :disabled="!imageSelected"
        title="Уже на 10 %"
        data-test="rte-image-smaller"
        @click="stepImageWidth(-1)"
      >
        −
      </button>
      <button
        type="button"
        class="rte-btn"
        :disabled="!imageSelected"
        title="Шире на 10 %"
        data-test="rte-image-bigger"
        @click="stepImageWidth(1)"
      >
        +
      </button>
      <button
        type="button"
        class="rte-btn"
        :disabled="!imageSelected"
        title="Половина ширины"
        data-test="rte-image-half"
        @click="setImageWidthShare(0.5)"
      >
        50 %
      </button>
      <button
        type="button"
        class="rte-btn"
        :disabled="!imageSelected"
        title="Во всю ширину"
        data-test="rte-image-full"
        @click="setImageWidthShare(1)"
      >
        100 %
      </button>
      <button
        type="button"
        class="rte-btn"
        :disabled="!imageSelected"
        title="Натуральный размер"
        data-test="rte-image-original"
        @click="applyImageWidth(null)"
      >
        как есть
      </button>

      <input
        ref="fileInput"
        type="file"
        accept="image/png,image/jpeg,image/gif,image/webp"
        multiple
        class="hidden"
        data-test="rte-file"
        @change="onFileInput"
      />
    </div>

    <EditorContent :editor="editor" />

    <p v-if="errorMessage" class="px-3 py-1 text-xs text-rose-700" data-test="rte-error">
      {{ errorMessage }}
    </p>

    <div
      v-if="drafts.length"
      class="flex flex-wrap gap-2 border-t border-slate-100 px-2 py-2"
      data-test="rte-images"
    >
      <div
        v-for="draft in drafts"
        :key="draft.name"
        class="flex items-center gap-2 rounded border border-slate-200 px-2 py-1"
        :data-test="`rte-image-${draft.name}`"
      >
        <img :src="draft.dataUrl" class="h-8 w-8 rounded object-cover" :alt="draft.sourceName" />
        <span class="magaz-muted text-xs">
          {{ draft.sourceName }} · {{ Math.round(draft.sizeBytes / 1024) }} КБ
        </span>
        <button
          type="button"
          class="magaz-muted text-xs hover:underline"
          data-test="rte-image-remove"
          @click="removeDraft(draft.name)"
        >
          убрать
        </button>
      </div>
    </div>

    <p class="magaz-muted px-3 py-2 text-xs">
      Картинка уедет вместе с уведомлением и останется на кассе в локальном кэше (§7.4). Внешние
      адреса в теле не рисуются: касса в интернет не ходит — ссылки давайте текстом.
    </p>
  </div>
</template>
