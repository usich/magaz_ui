<script setup lang="ts">
/**
 * Карточка магазина (§11.2, Э2): реквизиты, статус, живой `GET /health` агента
 * (кнопка «Проверить сейчас», Д-3), учётные данные 1С (П-1) и перевыпуск токена.
 *
 * Данные, которых нет в таблице `stores` (`busy`, сессии, рабочие пути),
 * показываются из ответа агента и живут только в этом окне: сервер их не
 * кэширует, чтобы не расходиться с фактом.
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'

import { ApiError } from '@/api/client'
import type { StoreHealth } from '@/api/types'
import OperationPanel from '@/components/OperationPanel.vue'
import StoreDevicesPanel from '@/components/StoreDevicesPanel.vue'
import { JOB_KIND_LABELS, JOB_STATUS_LABELS, jobStatusSeverity, useJobsStore } from '@/stores/jobs'
import { useStoresStore } from '@/stores/stores'

const route = useRoute()
const toast = useToast()
const stores = useStoresStore()
const jobs = useJobsStore()

const storeId = computed(() => Number(route.params.id))
const store = computed(() => stores.byId(storeId.value))

const health = ref<StoreHealth | null>(null)
const healthError = ref('')
const isChecking = ref(false)

const credentials = reactive({ user_name_1c: '', password: '' })
const isSavingCredentials = ref(false)

const tokenResult = ref<{ store_id: number; agent_token: string; warning: string } | null>(null)
const isTokenOpen = ref(false)

const isBusy = computed(() => Boolean(health.value?.busy.merge || health.value?.busy.update))

onMounted(async () => {
  if (!stores.items.length) {
    await stores.load()
  }
  if (store.value) {
    credentials.user_name_1c = store.value.user_name_1c
  }
  jobs.storeId = storeId.value
  await jobs.load()
})

function kindLabel(kind: string): string {
  return JOB_KIND_LABELS[kind] ?? kind
}

function statusLabel(status: string): string {
  return JOB_STATUS_LABELS[status] ?? status
}

/** После запуска операции перечитываем историю: задача уже в очереди (§11.6). */
async function reloadJobs(): Promise<void> {
  jobs.storeId = storeId.value
  await jobs.load()
}

function notifyError(error: unknown, fallback: string): void {
  const message = error instanceof ApiError ? error.message : fallback
  toast.add({ severity: 'error', summary: 'Операция не выполнена', detail: message, life: 6000 })
}

async function checkNow(): Promise<void> {
  isChecking.value = true
  healthError.value = ''
  try {
    health.value = await stores.checkHealth(storeId.value)
    credentials.user_name_1c = store.value?.user_name_1c ?? credentials.user_name_1c
  } catch (error) {
    health.value = null
    healthError.value = error instanceof ApiError ? error.message : 'Магазин не ответил'
  } finally {
    isChecking.value = false
  }
}

async function saveCredentials(): Promise<void> {
  if (!credentials.user_name_1c.trim()) {
    notifyError(new Error('Логин 1С не может быть пустым'), 'Логин 1С не может быть пустым')
    return
  }

  isSavingCredentials.value = true
  try {
    await stores.setCredentials(storeId.value, {
      user_name_1c: credentials.user_name_1c.trim(),
      ...(credentials.password ? { password: credentials.password } : {}),
    })
    credentials.password = ''
    toast.add({ severity: 'success', summary: 'Учётные данные сохранены', life: 4000 })
  } catch (error) {
    notifyError(error, 'Не удалось сохранить учётные данные')
  } finally {
    isSavingCredentials.value = false
  }
}

async function clearPassword(): Promise<void> {
  try {
    await stores.setCredentials(storeId.value, { clear_password: true })
    credentials.password = ''
    toast.add({ severity: 'success', summary: 'Пароль 1С очищен', life: 4000 })
  } catch (error) {
    notifyError(error, 'Не удалось очистить пароль')
  }
}

async function rotateToken(): Promise<void> {
  try {
    tokenResult.value = await stores.rotateToken(storeId.value)
    isTokenOpen.value = true
  } catch (error) {
    notifyError(error, 'Не удалось перевыпустить токен')
  }
}

function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU')
}
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center gap-3">
      <RouterLink :to="{ name: 'stores' }" class="magaz-muted text-sm hover:text-slate-900">
        ← Все магазины
      </RouterLink>
      <h2 class="text-lg font-semibold" data-test="store-title">
        {{ store?.name ?? `Магазин id=${storeId}` }}
      </h2>
      <Tag
        v-if="store"
        :value="store.is_online ? 'онлайн' : 'офлайн'"
        :severity="store.is_online ? 'success' : 'danger'"
        data-test="store-status"
      />
      <Tag v-if="store && !store.is_active" value="отключён" severity="secondary" />

      <div class="ml-auto flex gap-2">
        <Button
          label="Проверить сейчас"
          icon="pi pi-bolt"
          size="small"
          :loading="isChecking"
          data-test="check-store"
          @click="checkNow"
        />
        <Button
          label="Перевыпустить токен"
          icon="pi pi-key"
          size="small"
          text
          data-test="rotate-token"
          @click="rotateToken"
        />
      </div>
    </div>

    <Message v-if="stores.errorMessage" severity="error" :closable="false">
      {{ stores.errorMessage }}
    </Message>
    <Message v-if="healthError" severity="warn" :closable="false" data-test="health-error">
      {{ healthError }}
    </Message>

    <section v-if="store" class="grid gap-4 lg:grid-cols-2">
      <div class="rounded-xl border border-slate-200 bg-white p-5">
        <h3 class="font-semibold">Реквизиты</h3>
        <dl class="mt-3 space-y-2 text-sm">
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">IP / порт агента</dt>
            <dd class="magaz-mono">{{ store.ip_address }}:{{ store.agent_port }}</dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">Кластер (сервер 1С)</dt>
            <dd>{{ store.cluster_name }}</dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">База 1С</dt>
            <dd>{{ store.database_name }}</dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">Версия 1С</dt>
            <dd class="magaz-mono">{{ store.version_1c }}</dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">Пользователь 1С</dt>
            <dd>{{ store.user_name_1c }}</dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">Пароль 1С</dt>
            <dd>
              <Tag
                :value="store.password_set ? 'задан' : 'не задан'"
                :severity="store.password_set ? 'success' : 'secondary'"
                data-test="password-state"
              />
            </dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">Версии данных</dt>
            <dd class="magaz-mono">
              учётные данные {{ store.credentials_version }} · оборудование
              {{ store.devices_version }}
            </dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">Комментарий</dt>
            <dd>{{ store.comment ?? '—' }}</dd>
          </div>
        </dl>
      </div>

      <div class="rounded-xl border border-slate-200 bg-white p-5">
        <h3 class="font-semibold">Состояние канала связи</h3>
        <dl class="mt-3 space-y-2 text-sm">
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">Последний ответ агента</dt>
            <dd>{{ formatTime(store.last_seen_at) }}</dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">Версия агента</dt>
            <dd class="magaz-mono">{{ store.agent_version ?? '—' }}</dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">ОС кассы</dt>
            <dd>{{ store.os_info ?? '—' }}</dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">Операции 1С</dt>
            <dd>
              <Tag
                :value="isBusy ? 'выполняется' : 'свободен'"
                :severity="isBusy ? 'warn' : 'success'"
                data-test="busy-state"
              />
            </dd>
          </div>
        </dl>
        <p class="magaz-muted mt-3 text-xs">
          Операции, сессии и рабочие пути приходят только по кнопке «Проверить сейчас» (живой
          <span class="magaz-mono">GET /health</span>), поэтому в таблице `stores` их нет.
        </p>
      </div>
    </section>

    <section v-if="health" class="rounded-xl border border-slate-200 bg-white p-5" data-test="health-panel">
      <h3 class="font-semibold">Ответ агента ({{ formatTime(health.checked_at) }})</h3>
      <dl class="mt-3 grid gap-2 text-sm md:grid-cols-2">
        <div class="flex justify-between gap-4">
          <dt class="magaz-muted">Задержка ответа</dt>
          <dd>{{ health.latency_ms ?? '—' }} мс</dd>
        </div>
        <div class="flex justify-between gap-4">
          <dt class="magaz-muted">Аптайм службы</dt>
          <dd>{{ Math.round(health.uptime_sec / 60) }} мин</dd>
        </div>
        <div class="flex justify-between gap-4">
          <dt class="magaz-muted">Рабочая папка</dt>
          <dd class="magaz-mono">{{ health.work_dir ?? '—' }}</dd>
        </div>
        <div class="flex justify-between gap-4">
          <dt class="magaz-muted">Платформа 1С</dt>
          <dd class="magaz-mono">{{ health.base_1c_path ?? '—' }}</dd>
        </div>
        <div class="flex justify-between gap-4">
          <dt class="magaz-muted">Сессии пользователей</dt>
          <dd>
            <span v-if="health.sessions.length">
              {{ health.sessions.map((item) => item.user).join(', ') }}
            </span>
            <span v-else class="magaz-muted">нет активных сессий</span>
          </dd>
        </div>
        <div class="flex justify-between gap-4">
          <dt class="magaz-muted">Версии на агенте</dt>
          <dd class="magaz-mono">
            учётные данные {{ health.credentials_version }} · оборудование
            {{ health.devices_version }}
          </dd>
        </div>
      </dl>
      <p v-if="health.credentials_push_required" class="mt-3 text-xs text-amber-700">
        Версия учётных данных 1С на агенте отличается: сервер дослает пароль push-ом
        <span class="magaz-mono">POST /credentials</span> (П-1, этап Э4).
      </p>
      <p v-if="health.devices_push_required" class="mt-1 text-xs text-amber-700">
        Версия списка оборудования на агенте отличается: диспетчер дослает список
        <span class="magaz-mono">POST /devices/config</span> при следующей сверке (Д-1).
      </p>
    </section>

    <section v-if="store" class="rounded-xl border border-slate-200 bg-white p-5">
      <h3 class="font-semibold">Учётные данные 1С (П-1)</h3>
      <p class="magaz-muted mt-1 text-sm">
        Пароль хранится на сервере в шифрованном виде и никогда не показывается: в интерфейсе
        виден только признак «задан».
      </p>
      <div class="mt-3 grid gap-3 md:grid-cols-3">
        <label class="space-y-1">
          <span class="magaz-muted text-xs">Логин 1С</span>
          <InputText
            v-model="credentials.user_name_1c"
            class="w-full"
            :pt="{ root: { 'data-test': 'credentials-user' } }"
          />
        </label>
        <label class="space-y-1">
          <span class="magaz-muted text-xs">Новый пароль</span>
          <InputText
            v-model="credentials.password"
            type="password"
            class="w-full"
            placeholder="оставьте пустым, чтобы не менять"
            :pt="{ root: { 'data-test': 'credentials-password' } }"
          />
        </label>
        <div class="flex items-end gap-2">
          <Button
            label="Сохранить"
            icon="pi pi-save"
            size="small"
            :loading="isSavingCredentials"
            data-test="save-credentials"
            @click="saveCredentials"
          />
          <Button
            label="Очистить пароль"
            size="small"
            text
            severity="danger"
            data-test="clear-password"
            @click="clearPassword"
          />
        </div>
      </div>
    </section>

    <StoreDevicesPanel v-if="store" :store-id="storeId" />

    <OperationPanel
      :store-ids="[storeId]"
      title="Операции магазина"
      @submitted="reloadJobs"
    />

    <section class="rounded-xl border border-slate-200 bg-white p-5">
      <header class="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 class="font-semibold">История задач магазина</h3>
          <p class="magaz-muted text-xs">
            Последние операции с логами; полный лог открывается в задаче (§11.6).
          </p>
        </div>
        <div class="flex items-center gap-2">
          <span v-if="jobs.lastLoadedAt" class="magaz-muted text-xs">
            обновлено {{ jobs.lastLoadedAt.toLocaleTimeString('ru-RU') }}
          </span>
          <Button
            label="Обновить"
            icon="pi pi-refresh"
            size="small"
            text
            :loading="jobs.isLoading"
            data-test="store-jobs-refresh"
            @click="reloadJobs"
          />
        </div>
      </header>

      <div class="mt-3 overflow-x-auto">
        <table class="magaz-table">
          <thead>
            <tr>
              <th>#</th>
              <th>Операция</th>
              <th>Статус</th>
              <th>Автор</th>
              <th>Создана</th>
              <th>Последнее сообщение</th>
              <th />
            </tr>
          </thead>
          <tbody>
            <tr v-for="job in jobs.items" :key="job.id" :data-test="`store-job-${job.id}`">
              <td class="magaz-mono">{{ job.id }}</td>
              <td>{{ kindLabel(job.kind) }}</td>
              <td>
                <Tag
                  :value="statusLabel(job.status)"
                  :severity="jobStatusSeverity(job.status)"
                />
              </td>
              <td>{{ job.created_by ?? '—' }}</td>
              <td class="magaz-muted whitespace-nowrap">{{ formatTime(job.created_at) }}</td>
              <td class="max-w-sm truncate" :title="job.last_message ?? job.last_error ?? ''">
                {{ job.last_message ?? job.last_error ?? '—' }}
              </td>
              <td>
                <RouterLink :to="{ name: 'job-detail', params: { id: job.id } }">
                  <Button label="Лог" icon="pi pi-list" size="small" text />
                </RouterLink>
              </td>
            </tr>
            <tr v-if="!jobs.items.length">
              <td colspan="7" class="magaz-muted py-6 text-center" data-test="store-jobs-empty">
                Операций ещё не было: запустите проверку файлов или слияние выше.
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

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
        <Button label="Готово" size="small" @click="isTokenOpen = false" />
      </template>
    </Dialog>
  </div>
</template>
