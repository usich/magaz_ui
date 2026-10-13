<script setup lang="ts">
/**
 * Магазины (§11.2, Э2): таблица с фильтрами, CRUD по колонкам ТЗ, выдача и
 * перевыпуск токена агента, учётные данные 1С (П-1) и блок «что вписать в
 * конфиг агента». Статусы «онлайн/офлайн» приходят с сервера, который считает
 * их по окну `online_window_sec` (§3.3).
 */
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'

import { ApiError } from '@/api/client'
import type { AgentConfigBlock, StoreCreatePayload, StoreItem, StoreUpdatePayload } from '@/api/types'
import OperationPanel from '@/components/OperationPanel.vue'
import { useStoresStore } from '@/stores/stores'

const toast = useToast()
const stores = useStoresStore()

function emptyForm(): StoreCreatePayload {
  return {
    name: '',
    ip_address: '',
    cluster_name: '',
    database_name: '',
    version_1c: '',
    user_name_1c: 'Администратор',
    agent_port: 8383,
    password: '',
    comment: '',
  }
}

const form = reactive<StoreCreatePayload>(emptyForm())
const editingId = ref<number | null>(null)
const isFormOpen = ref(false)
const isSaving = ref(false)
const formError = ref('')

const configBlock = ref<AgentConfigBlock | null>(null)
const isConfigOpen = ref(false)
const tokenResult = ref<{ store_id: number; agent_token: string; warning: string } | null>(null)
const isTokenOpen = ref(false)

// Массовые операции (§11.4): «все магазины» или один кластер целиком
const isOperationsOpen = ref(false)
const operationsTarget = ref<'all' | 'cluster'>('all')
const operationsCluster = ref('')

const dialogTitle = computed(() =>
  editingId.value === null ? 'Новый магазин' : 'Изменить магазин',
)
const isFormValid = computed(
  () =>
    Boolean(form.name.trim()) &&
    Boolean(form.ip_address.trim()) &&
    Boolean(form.cluster_name.trim()) &&
    Boolean(form.database_name.trim()) &&
    Boolean(form.version_1c.trim()) &&
    Boolean(form.user_name_1c.trim()),
)
const configJson = computed(() =>
  configBlock.value ? JSON.stringify(configBlock.value.config_json, null, 2) : '',
)

onMounted(() => stores.startPolling())
onUnmounted(() => stores.stopPolling())

function notifyError(error: unknown, fallback: string): void {
  const message =
    error instanceof ApiError && error.messages.length > 0
      ? error.messages.join('; ')
      : error instanceof ApiError
        ? error.message
        : fallback
  toast.add({ severity: 'error', summary: 'Операция не выполнена', detail: message, life: 6000 })
}

function notifySuccess(summary: string, detail: string): void {
  toast.add({ severity: 'success', summary, detail, life: 4000 })
}

function openCreate(): void {
  Object.assign(form, emptyForm())
  editingId.value = null
  formError.value = ''
  isFormOpen.value = true
}

function openEdit(store: StoreItem): void {
  Object.assign(form, {
    name: store.name,
    ip_address: store.ip_address,
    cluster_name: store.cluster_name,
    database_name: store.database_name,
    version_1c: store.version_1c,
    user_name_1c: store.user_name_1c,
    agent_port: store.agent_port,
    password: '',
    comment: store.comment ?? '',
  })
  editingId.value = store.id
  formError.value = ''
  isFormOpen.value = true
}

async function submit(): Promise<void> {
  if (!isFormValid.value) {
    formError.value =
      'Заполните обязательные поля: имя, IP, кластер, база, версия 1С, пользователь 1С'
    return
  }

  isSaving.value = true
  formError.value = ''
  try {
    if (editingId.value === null) {
      const created = await stores.create({ ...form, password: form.password || null })
      configBlock.value = created.agent_config
      isConfigOpen.value = true
      notifySuccess('Магазин создан', 'Впишите данные в конфиг агента')
    } else {
      // Пароль 1С меняется только через `PUT /stores/{id}/credentials` (П-1)
      const payload: StoreUpdatePayload = {
        name: form.name,
        ip_address: form.ip_address,
        cluster_name: form.cluster_name,
        database_name: form.database_name,
        version_1c: form.version_1c,
        user_name_1c: form.user_name_1c,
        agent_port: form.agent_port,
        comment: form.comment,
      }
      await stores.update(editingId.value, payload)
      notifySuccess('Магазин сохранён', form.name)
    }
    isFormOpen.value = false
  } catch (error) {
    formError.value =
      error instanceof ApiError && error.messages.length > 0
        ? error.messages.join('; ')
        : 'Не удалось сохранить магазин'
    notifyError(error, 'Не удалось сохранить магазин')
  } finally {
    isSaving.value = false
  }
}

async function deactivate(store: StoreItem): Promise<void> {
  try {
    await stores.remove(store.id)
    notifySuccess('Магазин отключён', `${store.name} исключён из мониторинга`)
  } catch (error) {
    notifyError(error, 'Не удалось отключить магазин')
  }
}

async function reactivate(store: StoreItem): Promise<void> {
  try {
    await stores.update(store.id, { is_active: true })
    notifySuccess('Магазин включён', store.name)
  } catch (error) {
    notifyError(error, 'Не удалось включить магазин')
  }
}

async function rotateToken(store: StoreItem): Promise<void> {
  try {
    tokenResult.value = await stores.rotateToken(store.id)
    isTokenOpen.value = true
  } catch (error) {
    notifyError(error, 'Не удалось перевыпустить токен')
  }
}

async function copyText(value: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(value)
    notifySuccess('Скопировано', 'Значение в буфере обмена')
  } catch {
    toast.add({ severity: 'warn', summary: 'Скопируйте вручную', detail: value, life: 8000 })
  }
}

function formatTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU')
}
</script>

<template>
  <div class="space-y-4">
    <Message v-if="stores.errorMessage" severity="error" :closable="false">
      {{ stores.errorMessage }}
    </Message>

    <div class="flex flex-wrap items-center gap-3">
      <InputText
        v-model="stores.search"
        placeholder="Поиск: имя, IP, база, кластер"
        class="w-80"
        data-test="store-search"
        @keyup.enter="stores.load()"
      />
      <select
        v-model="stores.cluster"
        class="rounded-lg border border-slate-300 px-3 py-2 text-sm"
        data-test="store-cluster-filter"
        @change="stores.load()"
      >
        <option value="">Все кластеры</option>
        <option v-for="item in stores.clusters" :key="item" :value="item">{{ item }}</option>
      </select>
      <select
        v-model="stores.status"
        class="rounded-lg border border-slate-300 px-3 py-2 text-sm"
        data-test="store-status-filter"
        @change="stores.load()"
      >
        <option value="all">Все статусы</option>
        <option value="online">Только онлайн</option>
        <option value="offline">Только офлайн</option>
      </select>
      <label class="magaz-muted flex items-center gap-2 text-sm">
        <input
          v-model="stores.includeInactive"
          type="checkbox"
          data-test="store-include-inactive"
          @change="stores.load()"
        />
        показывать отключённые
      </label>

      <div class="ml-auto flex items-center gap-3">
        <span class="magaz-muted text-xs">
          Онлайн: <strong class="text-emerald-600">{{ stores.onlineCount }}</strong> · Офлайн:
          <strong class="text-rose-600">{{ stores.offlineCount }}</strong>
        </span>
        <Button
          label="Обновить"
          icon="pi pi-refresh"
          size="small"
          text
          :loading="stores.isLoading"
          data-test="refresh-stores"
          @click="stores.load()"
        />
        <Button
          label="Массовые операции"
          icon="pi pi-sitemap"
          size="small"
          severity="secondary"
          data-test="open-bulk-operations"
          @click="isOperationsOpen = true"
        />
        <Button
          label="Добавить магазин"
          icon="pi pi-plus"
          size="small"
          data-test="add-store"
          @click="openCreate"
        />
      </div>
    </div>

    <div class="overflow-hidden rounded-xl border border-slate-200 bg-white">
      <table class="magaz-table">
        <thead>
          <tr>
            <th>Магазин</th>
            <th class="w-40">IP / порт</th>
            <th class="w-40">Кластер</th>
            <th class="w-40">База</th>
            <th class="w-32">Версия 1С</th>
            <th class="w-28">Статус</th>
            <th class="w-44">Последняя активность</th>
            <th class="w-64">Действия</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="store in stores.items" :key="store.id" data-test="store-row">
            <td>
              <RouterLink :to="{ name: 'store-detail', params: { id: store.id } }" class="font-medium">
                {{ store.name }}
              </RouterLink>
              <p v-if="!store.is_active" class="magaz-muted text-xs">отключён</p>
            </td>
            <td class="magaz-mono">{{ store.ip_address }}:{{ store.agent_port }}</td>
            <td>{{ store.cluster_name }}</td>
            <td>{{ store.database_name }}</td>
            <td class="magaz-mono">{{ store.version_1c }}</td>
            <td data-test="store-status">
              <Tag
                :value="store.is_online ? 'онлайн' : 'офлайн'"
                :severity="store.is_online ? 'success' : 'danger'"
              />
            </td>
            <td class="magaz-muted">
              {{ formatTime(store.last_seen_at) }}
              <span v-if="store.agent_version" class="magaz-mono block text-xs">
                агент {{ store.agent_version }}
              </span>
            </td>
            <td>
              <div class="flex flex-wrap gap-1">
                <RouterLink
                  :to="{ name: 'store-detail', params: { id: store.id } }"
                  class="rounded px-2 py-1 text-sm text-blue-700 hover:bg-blue-50"
                  data-test="open-store"
                >
                  Карточка
                </RouterLink>
                <Button label="Правка" size="small" text @click="openEdit(store)" />
                <Button label="Токен" size="small" text @click="rotateToken(store)" />
                <Button
                  v-if="store.is_active"
                  label="Отключить"
                  size="small"
                  text
                  severity="danger"
                  @click="deactivate(store)"
                />
                <Button v-else label="Включить" size="small" text @click="reactivate(store)" />
              </div>
            </td>
          </tr>
          <tr v-if="!stores.items.length && !stores.isLoading">
            <td colspan="8" class="magaz-muted py-6 text-center">
              Магазинов нет. Нажмите «Добавить магазин» — сервер выдаст токен для агента.
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <p class="magaz-muted text-xs">
      «Онлайн» — успешный <span class="magaz-mono">GET /health</span> агента за окно
      <span class="magaz-mono">online_window_sec</span> (настройка сервера, §4.9): статус считает
      сервер и обновляет каждые 15 с, страница только показывает результат.
    </p>

    <Dialog v-model:visible="isFormOpen" :header="dialogTitle" modal :style="{ width: '720px' }">
      <Message v-if="formError" severity="error" :closable="false" class="mb-3">
        {{ formError }}
      </Message>

      <div class="grid gap-3 md:grid-cols-2">
        <label class="space-y-1">
          <span class="magaz-muted text-xs">Имя магазина *</span>
          <InputText
            v-model="form.name"
            class="w-full"
            :pt="{ root: { 'data-test': 'form-name' } }"
          />
        </label>
        <label class="space-y-1">
          <span class="magaz-muted text-xs">IP кассы в туннеле *</span>
          <InputText
            v-model="form.ip_address"
            class="w-full"
            placeholder="10.10.5.10"
            :pt="{ root: { 'data-test': 'form-ip' } }"
          />
        </label>
        <label class="space-y-1">
          <span class="magaz-muted text-xs">Кластер (сервер 1С) *</span>
          <InputText
            v-model="form.cluster_name"
            class="w-full"
            :pt="{ root: { 'data-test': 'form-cluster' } }"
          />
        </label>
        <label class="space-y-1">
          <span class="magaz-muted text-xs">Имя базы 1С *</span>
          <InputText
            v-model="form.database_name"
            class="w-full"
            :pt="{ root: { 'data-test': 'form-database' } }"
          />
        </label>
        <label class="space-y-1">
          <span class="magaz-muted text-xs">Версия 1С *</span>
          <InputText
            v-model="form.version_1c"
            class="w-full"
            placeholder="8.3.24.1548"
            :pt="{ root: { 'data-test': 'form-version' } }"
          />
        </label>
        <label class="space-y-1">
          <span class="magaz-muted text-xs">Порт агента</span>
          <input
            v-model.number="form.agent_port"
            type="number"
            min="1"
            max="65535"
            class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            data-test="form-port"
          />
        </label>
        <label class="space-y-1">
          <span class="magaz-muted text-xs">Пользователь 1С *</span>
          <InputText
            v-model="form.user_name_1c"
            class="w-full"
            :pt="{ root: { 'data-test': 'form-user' } }"
          />
        </label>
        <label class="space-y-1">
          <span class="magaz-muted text-xs">
            Пароль 1С {{ editingId === null ? '' : '(оставьте пустым, чтобы не менять)' }}
          </span>
          <InputText
            v-model="form.password"
            type="password"
            class="w-full"
            :pt="{ root: { 'data-test': 'form-password' } }"
          />
        </label>
        <label class="space-y-1 md:col-span-2">
          <span class="magaz-muted text-xs">Комментарий</span>
          <InputText
            v-model="form.comment"
            class="w-full"
            :pt="{ root: { 'data-test': 'form-comment' } }"
          />
        </label>
      </div>

      <p class="magaz-muted mt-4 text-xs">
        Пароль 1С хранится на сервере в шифрованном виде и доставляется агенту отдельно (П-1):
        в HTML, логах и аудите он не появляется — в таблице виден только признак «задан».
      </p>

      <template #footer>
        <Button label="Отмена" text size="small" @click="isFormOpen = false" />
        <Button
          label="Сохранить"
          icon="pi pi-save"
          size="small"
          :loading="isSaving"
          :disabled="!isFormValid"
          data-test="save-store"
          @click="submit"
        />
      </template>
    </Dialog>

    <Dialog
      v-model:visible="isConfigOpen"
      header="Что вписать в конфиг агента"
      modal
      :style="{ width: '760px' }"
    >
      <p class="text-sm">
        Файл: <span class="magaz-mono">{{ configBlock?.config_path }}</span>
      </p>
      <p class="magaz-muted mt-1 text-sm">
        Адрес сервера: <span class="magaz-mono">{{ configBlock?.server_url }}</span>
      </p>
      <pre
        class="magaz-mono mt-3 max-h-72 overflow-auto rounded-lg bg-slate-900 p-3 text-xs text-slate-100"
        data-test="agent-config-json"
        >{{ configJson }}</pre
      >
      <ul class="magaz-muted mt-3 space-y-1 text-xs">
        <li v-for="note in configBlock?.notes ?? []" :key="note">• {{ note }}</li>
      </ul>

      <template #footer>
        <Button
          label="Скопировать токен"
          icon="pi pi-copy"
          size="small"
          text
          @click="copyText(configBlock?.agent_token ?? '')"
        />
        <Button
          label="Скопировать JSON"
          icon="pi pi-copy"
          size="small"
          data-test="copy-config"
          @click="copyText(configJson)"
        />
        <Button label="Готово" size="small" @click="isConfigOpen = false" />
      </template>
    </Dialog>

    <Dialog
      v-model:visible="isTokenOpen"
      header="Токен агента перевыпущен"
      modal
      :style="{ width: '620px' }"
    >
      <Message severity="warn" :closable="false">{{ tokenResult?.warning }}</Message>
      <p class="magaz-mono mt-3 break-all rounded-lg bg-slate-100 p-3 text-sm" data-test="new-token">
        {{ tokenResult?.agent_token }}
      </p>
      <template #footer>
        <Button
          label="Скопировать токен"
          icon="pi pi-copy"
          size="small"
          data-test="copy-token"
          @click="copyText(tokenResult?.agent_token ?? '')"
        />
        <Button label="Готово" size="small" text @click="isTokenOpen = false" />
      </template>
    </Dialog>

    <Dialog
      v-model:visible="isOperationsOpen"
      header="Массовые операции"
      modal
      :style="{ width: '900px' }"
      data-test="bulk-operations-dialog"
    >
      <p class="magaz-muted text-sm">
        Операция ставится всем активным магазинам или всему кластеру сразу (§11.4). Отключённые
        магазины пропускаются, о них сообщит прогресс.
      </p>

      <div class="mt-3 flex flex-wrap items-end gap-3">
        <label class="space-y-1">
          <span class="magaz-muted text-xs">Кому</span>
          <select
            v-model="operationsTarget"
            class="rounded-lg border border-slate-300 px-3 py-2 text-sm"
            data-test="bulk-operations-target"
          >
            <option value="all">Все активные магазины</option>
            <option value="cluster">Один кластер</option>
          </select>
        </label>
        <label v-if="operationsTarget === 'cluster'" class="space-y-1">
          <span class="magaz-muted text-xs">Кластер</span>
          <select
            v-model="operationsCluster"
            class="rounded-lg border border-slate-300 px-3 py-2 text-sm"
            data-test="bulk-operations-cluster"
          >
            <option value="">— выберите —</option>
            <option v-for="item in stores.clusters" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
      </div>

      <OperationPanel
        v-if="operationsTarget === 'all' || operationsCluster"
        class="mt-4"
        :all="operationsTarget === 'all'"
        :cluster="operationsTarget === 'cluster' ? operationsCluster : null"
        title="Операции"
        :show-download-link="true"
      />
      <Message v-else severity="info" :closable="false" class="mt-4">
        Выберите кластер, чтобы запустить массовую операцию.
      </Message>

      <template #footer>
        <Button label="Закрыть" size="small" text @click="isOperationsOpen = false" />
      </template>
    </Dialog>
  </div>
</template>
