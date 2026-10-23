<script setup lang="ts">
/**
 * Магазины (§11.2, Э2/Э8): таблица с фильтрами, CRUD по колонкам ТЗ, правка всех
 * реквизитов, выдача и перевыпуск токена агента, учётные данные 1С (П-1) и блок
 * «что вписать в конфиг агента» — его можно открыть и скачать в любой момент, а
 * не только сразу после создания магазина.
 *
 * Статусы «онлайн/офлайн» приходят с сервера, который считает их по окну
 * `online_window_sec` (§3.3). Форма магазина и диалог конфига — общие компоненты
 * (`StoreFormDialog`, `AgentConfigDialog`): те же открываются в карточке.
 */
import { onMounted, onUnmounted, ref } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'

import { ApiError } from '@/api/client'
import type { StoreItem } from '@/api/types'
import AgentConfigDialog from '@/components/AgentConfigDialog.vue'
import OperationPanel from '@/components/OperationPanel.vue'
import StoreFormDialog from '@/components/StoreFormDialog.vue'
import { useStoresStore } from '@/stores/stores'

const toast = useToast()
const stores = useStoresStore()

/** Диалог формы: `formStore === null` — создание, иначе правка реквизитов (§11.2) */
const formStore = ref<StoreItem | null>(null)
const isFormOpen = ref(false)

/** Диалог конфига агента: открывается кнопкой «Конфиг» и сразу после создания */
const configStore = ref<StoreItem | null>(null)
const isConfigOpen = ref(false)
const tokenResult = ref<{ store_id: number; agent_token: string; warning: string } | null>(null)
const isTokenOpen = ref(false)

// Массовые операции (§11.4): «все магазины» или один кластер целиком
const isOperationsOpen = ref(false)
const operationsTarget = ref<'all' | 'cluster'>('all')
const operationsCluster = ref('')

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
  formStore.value = null
  copyFrom.value = null
  isFormOpen.value = true
}

function openEdit(store: StoreItem): void {
  formStore.value = store
  copyFrom.value = null
  isFormOpen.value = true
}

/** «Скопировать» (Д-23): создать магазин по образцу — все поля, кроме имени. */
const copyFrom = ref<StoreItem | null>(null)
function openCopy(store: StoreItem): void {
  copyFrom.value = store
  formStore.value = null
  isFormOpen.value = true
}

/** Конфиг агента для уже созданного магазина (§11.2, Э8). */
function openConfig(store: StoreItem): void {
  configStore.value = store
  isConfigOpen.value = true
}

/**
 * Форма сохранила магазин: после создания сразу показываем `agent-config.json`,
 * после правки — только уведомление (конфиг открывается кнопкой «Конфиг»).
 */
function handleSaved(store: StoreItem, created: boolean): void {
  formStore.value = null
  if (created) {
    configStore.value = store
    isConfigOpen.value = true
    notifySuccess('Магазин создан', 'Впишите данные в конфиг агента')
    return
  }
  notifySuccess('Магазин сохранён', store.name)
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
                <Button label="Скопировать" size="small" text :data-test="`store-copy-${store.id}`" @click="openCopy(store)" />
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
                <Button
                  label="Конфиг"
                  size="small"
                  text
                  data-test="open-config"
                  @click="openConfig(store)"
                />
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

    <!-- Форма создания и правки: все реквизиты магазина (§11.2, Э8) -->
    <StoreFormDialog v-model:visible="isFormOpen" :store="formStore" :copy-from="copyFrom" @saved="handleSaved" />

    <!-- Конфиг агента: тот же блок, что показывается сразу после создания -->
    <AgentConfigDialog
      v-model:visible="isConfigOpen"
      :store-id="configStore?.id ?? null"
      :store-name="configStore?.name ?? null"
    />

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
