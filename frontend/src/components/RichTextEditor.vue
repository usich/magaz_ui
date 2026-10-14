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
import Image from '@tiptap/extension-image'
import StarterKit from '@tiptap/starter-kit'
import TextAlign from '@tiptap/extension-text-align'
import { BackgroundColor, Color, TextStyle } from '@tiptap/extension-text-style'
import { EditorContent, useEditor } from '@tiptap/vue-3'

import {
  IMAGES_MAX_PER_NOTIFICATION,
  IMAGE_SRC_PREFIX,
  imageFileError,
  imageNameFromSrc,
  makeImageName,
  notificationImageUrl,
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
const fileInput = ref<HTMLInputElement | null>(null)
const drafts = computed(() => props.drafts)

/** Адрес картинки для редактора: черновик до сохранения, эндпоинт после (§7.4). */
function resolveImage(name: string): string {
  const draft = drafts.value.find((item) => item.name === name)
  return draft?.dataUrl ?? notificationImageUrl(props.notificationId, name)
}

/**
 * Узел картинки: в документе живёт метка `magaz-image:<имя>`, а на экране —
 * сама картинка. Иначе админ видел бы «пропавшее изображение»: файла на сервере
 * ещё нет, уведомление не создано.
 */
const MagazImage = Image.extend({
  addNodeView() {
    return ({ node, getPos, editor }) => {
      const wrapper = document.createElement('span')
      wrapper.className = 'magaz-image-node'
      const picture = document.createElement('img')
      const remove = document.createElement('button')
      remove.type = 'button'
      remove.className = 'magaz-image-remove'
      remove.title = 'Убрать картинку'
      remove.textContent = '✕'

      let current = node
      const render = (): void => {
        const name = imageNameFromSrc(current.attrs.src as string)
        picture.alt = String(current.attrs.alt ?? name ?? 'картинка')
        const src = name ? resolveImage(name) : ''
        if (src) picture.src = src
        else picture.removeAttribute('src')
      }
      render()
      remove.addEventListener('click', () => {
        const position = typeof getPos === 'function' ? getPos() : null
        if (position === null || position === undefined) return
        editor.chain().focus().deleteRange({ from: position, to: position + current.nodeSize }).run()
      })
      wrapper.append(picture, remove)
      return {
        dom: wrapper,
        update: (updated) => {
          if (updated.type.name !== current.type.name) return false
          current = updated
          render()
          return true
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
 * Разложить файлы по черновикам и поставить в тело по узлу на каждый.
 *
 * Картинка превращается в `data:`-URL: так её видно в редакторе и предпросмотре,
 * а на сервер она уедет тем же телом (`data_base64`) при создании уведомления.
 */
async function stageFiles(files: File[]): Promise<void> {
  const images = files.filter((file) => file.type.startsWith('image/'))
  if (!images.length) return
  const staged = [...drafts.value]
  for (const file of images) {
    if (staged.length >= IMAGES_MAX_PER_NOTIFICATION) {
      errorMessage.value =
        `К одному уведомлению можно приложить не больше ` +
        `${IMAGES_MAX_PER_NOTIFICATION} картинок`
      break
    }
    const reason = imageFileError(file)
    if (reason) {
      errorMessage.value = `${file.name}: ${reason}`
      continue
    }
    const name = makeImageName(file.name)
    staged.push({
      name,
      contentType: file.type,
      dataUrl: await readAsDataUrl(file),
      sizeBytes: file.size,
      sourceName: file.name,
    })
    editor.value
      ?.chain()
      .focus()
      .setImage({ src: `${IMAGE_SRC_PREFIX}${name}`, alt: file.name })
      .run()
  }
  // Ничего не приняли — не считаем это изменением: панель не должна «мигать» (Э9)
  if (staged.length !== drafts.value.length) emit('update:drafts', staged)
}

/** Картинки из буфера обмена или перетаскивания: тот же путь, что у выбора файла. */
function insertFromData(data: DataTransfer | null): boolean {
  const files = Array.from(data?.files ?? []).filter((file) => file.type.startsWith('image/'))
  if (!files.length) return false
  void stageFiles(files)
  return true
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

/** Состояние кнопок читается из редактора — панель показывает активные форматы (§11.5). */
const can = computed(() => editor.value?.isEditable ?? false)

/** `isActive` принимает и имя формата, и набор атрибутов (например выравнивание). */
function isActive(
  name: string | Record<string, unknown>,
  attrs?: Record<string, unknown>,
): boolean {
  const instance = editor.value
  if (!instance) return false
  return typeof name === 'string' ? instance.isActive(name, attrs) : instance.isActive(name)
}

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
