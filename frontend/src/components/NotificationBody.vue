<script setup lang="ts">
/**
 * Тело уведомления «как увидит кассир» (§4.6, §11.5, Э9).
 *
 * Разметку чистит сервер — единственная точка доверия (§4.6), поэтому здесь она
 * только показывается: метка `magaz-image:<имя>` превращается в адрес картинки.
 * Пока уведомление не создано, файла на сервере ещё нет, и адрес берётся из
 * черновиков редактора (`drafts`) — предпросмотр показывает то же сообщение,
 * которое уйдёт на кассу, а не «похожее».
 */
import { computed } from 'vue'

import {
  imageNameFromSrc,
  notificationImageUrl,
  type NotificationImageDraft,
} from '@/stores/notifications'

const props = withDefaults(
  defineProps<{
    body: string
    /** `plain` — простой текст, `html` — разметка из редактора (§4.6). */
    bodyFormat?: string
    notificationId?: number | null
    /** Черновики картинок: `data:`-URL по имени — для предпросмотра до сохранения. */
    drafts?: NotificationImageDraft[]
    /** Компактный вид: для карточек и списков (меньше отступы, тот же текст). */
    compact?: boolean
  }>(),
  { bodyFormat: 'plain', notificationId: null, drafts: () => [], compact: false },
)

/** Имя → картинка-черновик: у сохранённого уведомления карта пустая. */
const draftSources = computed(() => {
  const sources = new Map<string, string>()
  for (const draft of props.drafts) sources.set(draft.name, draft.dataUrl)
  return sources
})

/**
 * Готовый HTML: у простого тела — экранированный текст, у разметки — метки,
 * заменённые на адрес картинки. Метка без картинки убирается целиком: «сломанное
 * изображение» в предпросмотре ничего не объясняет (§4.6).
 */
const html = computed(() => {
  if (props.bodyFormat !== 'html') return escapeHtml(props.body).replace(/\n/g, '<br>')
  const parsed = new DOMParser().parseFromString(props.body, 'text/html')
  parsed.querySelectorAll('img').forEach((node) => {
    const name = imageNameFromSrc(node.getAttribute('src'))
    const source = name
      ? draftSources.value.get(name) ?? notificationImageUrl(props.notificationId, name)
      : ''
    if (!source) {
      node.remove()
      return
    }
    node.setAttribute('src', source)
    if (!node.getAttribute('alt')) node.setAttribute('alt', name ?? 'картинка')
  })
  parsed.querySelectorAll('a').forEach((node) => {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  })
  return parsed.body?.innerHTML ?? ''
})

/** Экранировать текст: у простого тела `<` — это `<`, а не разметка. */
function escapeHtml(value: string): string {
  const element = document.createElement('div')
  element.textContent = value
  return element.innerHTML
}
</script>

<template>
  <div
    class="magaz-richtext"
    :class="compact ? 'magaz-richtext-compact' : ''"
    data-test="notification-body"
    v-html="html"
  />
</template>
