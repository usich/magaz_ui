<script setup lang="ts">
/**
 * Диалог «Что вписать в конфиг агента» (§11.2, §7.2, Э8).
 *
 * Один компонент на страницу `/stores` и карточку магазина: конфиг читается
 * запросом `GET /admin/stores/{id}/agent-config`, поэтому его можно показать в
 * любой момент, а не только сразу после создания магазина — например, когда
 * кассу переустанавливают или токен перевыпустили.
 *
 * Пароль 1С в `agent-config.json` не попадает (П-1): его агент получает push-ом,
 * поэтому файл можно копировать и переносить без риска утечки пароля.
 */
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import Message from 'primevue/message'
import { useToast } from 'primevue/usetoast'

import { ApiError } from '@/api/client'
import type { AgentConfigBlock } from '@/api/types'
import { AGENT_CONFIG_FILE_NAME, useStoresStore } from '@/stores/stores'

const props = withDefaults(
  defineProps<{
    visible: boolean
    /** Магазин, для которого собирается конфиг; `null` — диалог пуст */
    storeId: number | null
    storeName?: string | null
  }>(),
  { storeName: null },
)

const emit = defineEmits<{
  'update:visible': [value: boolean]
}>()

const toast = useToast()
const stores = useStoresStore()

const config = ref<AgentConfigBlock | null>(null)
const isLoading = ref(false)
const errorMessage = ref('')

const configJson = computed(() =>
  config.value ? JSON.stringify(config.value.config_json, null, 2) : '',
)

/** Перечитать конфиг при каждом открытии: токен мог смениться. */
watch(
  () => props.visible,
  async (visible) => {
    if (!visible) return
    config.value = null
    errorMessage.value = ''
    if (props.storeId === null) return

    isLoading.value = true
    try {
      config.value = await stores.loadAgentConfig(props.storeId)
    } catch (error) {
      errorMessage.value =
        error instanceof ApiError ? error.message : 'Не удалось получить конфиг агента'
    } finally {
      isLoading.value = false
    }
  },
)

function close(): void {
  emit('update:visible', false)
}

async function copyText(value: string, what: string): Promise<void> {
  if (!value) return
  try {
    await navigator.clipboard.writeText(value)
    toast.add({ severity: 'success', summary: 'Скопировано', detail: what, life: 3000 })
  } catch {
    toast.add({ severity: 'warn', summary: 'Скопируйте вручную', detail: value, life: 8000 })
  }
}

/** Скачать ровно тот файл, который ждёт агент: `agent-config.json` (§7.2). */
function downloadConfig(): void {
  if (!configJson.value) return
  const blob = new Blob([configJson.value], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = AGENT_CONFIG_FILE_NAME
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
  toast.add({
    severity: 'success',
    summary: 'Файл сохранён',
    detail: `${AGENT_CONFIG_FILE_NAME}: положите его рядом с exe агента`,
    life: 5000,
  })
}
</script>

<template>
  <Dialog
    :visible="visible"
    header="Что вписать в конфиг агента"
    modal
    :style="{ width: '760px' }"
    data-test="agent-config-dialog"
    @update:visible="emit('update:visible', $event)"
  >
    <Message v-if="errorMessage" severity="error" :closable="false" data-test="agent-config-error">
      {{ errorMessage }}
    </Message>

    <template v-if="config">
      <p class="text-sm">
        Магазин:
        <strong>{{ storeName ?? config.config_json.store_name }}</strong>
      </p>
      <p class="mt-1 text-sm">
        Файл: <span class="magaz-mono">{{ config.config_path }}</span>
      </p>
      <p class="magaz-muted mt-1 text-sm">
        Адрес сервера: <span class="magaz-mono">{{ config.server_url }}</span> · порт агента
        <span class="magaz-mono">{{ config.port }}</span>
      </p>
      <pre
        class="magaz-mono mt-3 max-h-72 overflow-auto rounded-lg bg-slate-900 p-3 text-xs text-slate-100"
        data-test="agent-config-json"
        >{{ configJson }}</pre
      >
      <ul class="magaz-muted mt-3 space-y-1 text-xs">
        <li v-for="note in config.notes" :key="note">• {{ note }}</li>
      </ul>
    </template>

    <Message v-else-if="isLoading" severity="info" :closable="false">Читаем конфиг…</Message>
    <Message v-else-if="!errorMessage" severity="info" :closable="false">
      Выберите магазин, чтобы увидеть его `agent-config.json`.
    </Message>

    <template #footer>
      <Button
        label="Скопировать токен"
        icon="pi pi-copy"
        size="small"
        text
        :disabled="!config"
        data-test="copy-config-token"
        @click="copyText(config?.agent_token ?? '', 'Токен агента в буфере обмена')"
      />
      <Button
        label="Скопировать JSON"
        icon="pi pi-copy"
        size="small"
        :disabled="!config"
        data-test="copy-config"
        @click="copyText(configJson, 'Содержимое agent-config.json в буфере обмена')"
      />
      <Button
        label="Скачать agent-config.json"
        icon="pi pi-download"
        size="small"
        :disabled="!config"
        data-test="download-config"
        @click="downloadConfig"
      />
      <Button label="Готово" size="small" text @click="close" />
    </template>
  </Dialog>
</template>
