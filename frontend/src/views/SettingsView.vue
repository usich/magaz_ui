<script setup lang="ts">
/**
 * Настройки (§4.9, §11.6). Работает уже в Э1: значения читаются из БД и
 * меняются без перезапуска сервера — на них опираются мониторинг (Э2),
 * опрос оборудования (Э4Б) и возврат отложенных уведомлений (Э5).
 */
import { computed, onMounted, ref } from 'vue'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'

import { ApiError } from '@/api/client'
import { settingsApi } from '@/api/endpoints'
import type { SettingItem } from '@/api/types'

const toast = useToast()

const items = ref<SettingItem[]>([])
const draft = ref<Record<string, string>>({})
const isLoading = ref(false)
const isSaving = ref(false)
const errorMessage = ref('')

const changedKeys = computed(() =>
  items.value
    .filter((item) => draft.value[item.key] !== item.value)
    .map((item) => item.key),
)

const hasChanges = computed(() => changedKeys.value.length > 0)

onMounted(load)

async function load(): Promise<void> {
  isLoading.value = true
  try {
    items.value = await settingsApi.list()
    draft.value = Object.fromEntries(items.value.map((item) => [item.key, item.value]))
    errorMessage.value = ''
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : 'Не удалось загрузить настройки'
  } finally {
    isLoading.value = false
  }
}

async function save(): Promise<void> {
  if (!hasChanges.value) return

  isSaving.value = true
  try {
    const values = Object.fromEntries(changedKeys.value.map((key) => [key, draft.value[key]]))
    items.value = await settingsApi.update(values)
    draft.value = Object.fromEntries(items.value.map((item) => [item.key, item.value]))
    toast.add({ severity: 'success', summary: 'Настройки сохранены', detail: `${Object.keys(values).length} шт.`, life: 3000 })
  } catch (error) {
    const message =
      error instanceof ApiError && error.messages.length > 0
        ? error.messages.join('; ')
        : 'Не удалось сохранить настройки'
    errorMessage.value = message
    toast.add({ severity: 'error', summary: 'Ошибка сохранения', detail: message, life: 5000 })
  } finally {
    isSaving.value = false
  }
}

function restoreDefault(item: SettingItem): void {
  draft.value[item.key] = item.default
}
</script>

<template>
  <div class="space-y-4">
    <Message v-if="errorMessage" severity="error" :closable="false">{{ errorMessage }}</Message>

    <div class="flex items-center justify-between">
      <p class="magaz-muted text-sm">
        Изменения применяются без перезапуска сервера. Изменённых значений:
        <strong>{{ changedKeys.length }}</strong>
      </p>
      <div class="flex gap-2">
        <Button label="Отменить" size="small" text :disabled="!hasChanges" @click="load" />
        <Button
          label="Сохранить"
          icon="pi pi-save"
          size="small"
          :disabled="!hasChanges"
          :loading="isSaving"
          data-test="save-settings"
          @click="save"
        />
      </div>
    </div>

    <div class="overflow-hidden rounded-xl border border-slate-200 bg-white">
      <table class="magaz-table">
        <thead>
          <tr>
            <th class="w-64">Ключ</th>
            <th>Назначение</th>
            <th class="w-40">Значение</th>
            <th class="w-32">По умолчанию</th>
            <th class="w-24"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in items" :key="item.key">
            <td class="magaz-mono">{{ item.key }}</td>
            <td class="magaz-muted">{{ item.description }}</td>
            <td>
              <InputText
                v-model="draft[item.key]"
                class="w-full"
                :pt="{ root: { 'data-test': `setting-${item.key}` } }"
              />
            </td>
            <td>
              <Tag
                :value="draft[item.key] === item.default ? 'совпадает' : item.default"
                :severity="draft[item.key] === item.default ? 'success' : 'secondary'"
              />
            </td>
            <td>
              <Button
                v-if="draft[item.key] !== item.default"
                label="Сброс"
                size="small"
                text
                @click="restoreDefault(item)"
              />
            </td>
          </tr>
          <tr v-if="!items.length && !isLoading">
            <td colspan="5" class="magaz-muted py-6 text-center">Настройки не загружены</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
