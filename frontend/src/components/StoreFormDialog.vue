<script setup lang="ts">
/**
 * Форма магазина (§11.2, Э8): «Добавить магазин» и «Изменить магазин».
 *
 * Одна форма на страницу `/stores` и на карточку магазина. Правятся **все**
 * реквизиты: имя, IP, порт агента, кластер и база 1С, версия, логин 1С,
 * комментарий и признак «активен» (§4.1). Пароль 1С меняется только через
 * `PUT /stores/{id}/credentials` (П-1) — в конфиг агента и в ответы API он не
 * попадает, поэтому в форме это отдельные действия: «задать/заменить» или
 * «очистить».
 */
import { computed, reactive, ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import { useToast } from 'primevue/usetoast'

import { ApiError } from '@/api/client'
import type { StoreCreatePayload, StoreItem, StoreUpdatePayload } from '@/api/types'
import { DEFAULT_AGENT_PORT, useStoresStore } from '@/stores/stores'

const props = withDefaults(
  defineProps<{
    visible: boolean
    /** `null` — создание магазина, иначе — правка переданного (§11.2) */
    store?: StoreItem | null
  }>(),
  { store: null },
)

const emit = defineEmits<{
  'update:visible': [value: boolean]
  /** Сохранено: магазин из ответа API и признак «только что создан» */
  saved: [store: StoreItem, created: boolean]
}>()

interface StoreForm {
  name: string
  ip_address: string
  cluster_name: string
  database_name: string
  version_1c: string
  user_name_1c: string
  agent_port: number
  password: string
  comment: string
  extension_name: string
  is_active: boolean
  clear_password: boolean
  // Д-21: проверка бэкапа
  backup_check_enabled: boolean
  backup_path: string
  backup_host: string
  backup_use_auth: boolean
  backup_user: string
  backup_password: string
  backup_clear_password: boolean
  backup_check_yesterday: boolean
}

function emptyForm(): StoreForm {
  return {
    name: '',
    ip_address: '',
    cluster_name: '',
    database_name: '',
    version_1c: '',
    user_name_1c: 'Администратор',
    agent_port: DEFAULT_AGENT_PORT,
    password: '',
    comment: '',
    extension_name: '',
    is_active: true,
    clear_password: false,
    backup_check_enabled: false,
    backup_path: '',
    backup_host: '',
    backup_use_auth: false,
    backup_user: '',
    backup_password: '',
    backup_clear_password: false,
    backup_check_yesterday: false,
  }
}

const toast = useToast()
const stores = useStoresStore()

const form = reactive<StoreForm>(emptyForm())
const isSaving = ref(false)
const formError = ref('')

const isEdit = computed(() => props.store !== null)
const dialogTitle = computed(() => (isEdit.value ? 'Изменить магазин' : 'Новый магазин'))
const passwordSet = computed(() => Boolean(props.store?.password_set))
const isFormValid = computed(
  () =>
    Boolean(form.name.trim()) &&
    Boolean(form.ip_address.trim()) &&
    Boolean(form.cluster_name.trim()) &&
    Boolean(form.database_name.trim()) &&
    Boolean(form.version_1c.trim()) &&
    Boolean(form.user_name_1c.trim()) &&
    Number.isInteger(Number(form.agent_port)) &&
    Number(form.agent_port) >= 1 &&
    Number(form.agent_port) <= 65535,
)

/** Заполнить форму значениями по умолчанию или реквизитами магазина (правка). */
function reset(): void {
  const store = props.store
  Object.assign(form, emptyForm(), {
    name: store?.name ?? '',
    ip_address: store?.ip_address ?? '',
    cluster_name: store?.cluster_name ?? '',
    database_name: store?.database_name ?? '',
    version_1c: store?.version_1c ?? '',
    user_name_1c: store?.user_name_1c ?? 'Администратор',
    agent_port: store?.agent_port ?? DEFAULT_AGENT_PORT,
    comment: store?.comment ?? '',
    extension_name: store?.extension_name ?? '',
    is_active: store?.is_active ?? true,
    backup_check_enabled: store?.backup_check_enabled ?? false,
    backup_path: store?.backup_path ?? '',
    backup_host: store?.backup_host ?? '',
    backup_use_auth: store?.backup_use_auth ?? false,
    backup_user: store?.backup_user ?? '',
    backup_check_yesterday: store?.backup_check_yesterday ?? false,
  })
  formError.value = ''
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) reset()
  },
)

function close(): void {
  emit('update:visible', false)
}

function fail(error: unknown, fallback: string): void {
  const messages =
    error instanceof ApiError && error.messages.length > 0 ? error.messages.join('; ') : ''
  formError.value = messages || (error instanceof ApiError ? error.message : fallback)
  toast.add({
    severity: 'error',
    summary: 'Операция не выполнена',
    detail: formError.value,
    life: 6000,
  })
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
    if (!isEdit.value) {
      const payload: StoreCreatePayload = {
        name: form.name.trim(),
        ip_address: form.ip_address.trim(),
        cluster_name: form.cluster_name.trim(),
        database_name: form.database_name.trim(),
        version_1c: form.version_1c.trim(),
        user_name_1c: form.user_name_1c.trim(),
        agent_port: Number(form.agent_port),
        password: form.password || null,
        comment: form.comment.trim() || null,
        extension_name: form.extension_name.trim() || null,
        backup_check_enabled: form.backup_check_enabled,
        backup_path: form.backup_path.trim() || null,
        backup_host: form.backup_host.trim() || null,
        backup_use_auth: form.backup_use_auth,
        backup_user: form.backup_use_auth ? form.backup_user.trim() || null : null,
        backup_password: form.backup_use_auth ? form.backup_password || null : null,
        backup_clear_password: form.backup_clear_password,
        backup_check_yesterday: form.backup_check_yesterday,
      }
      const created = await stores.create(payload)
      emit('saved', created, true)
      close()
      return
    }

    const id = props.store?.id
    if (id === undefined) return

    // Реквизиты — одним `PUT`; пароль 1С и возврат в активные идут своими точками
    // контракта, чтобы не смешивать их с обычной правкой (§11.2, П-1)
    const payload: StoreUpdatePayload = {
      name: form.name.trim(),
      ip_address: form.ip_address.trim(),
      cluster_name: form.cluster_name.trim(),
      database_name: form.database_name.trim(),
      version_1c: form.version_1c.trim(),
      user_name_1c: form.user_name_1c.trim(),
      agent_port: Number(form.agent_port),
      comment: form.comment.trim() || null,
      extension_name: form.extension_name.trim() || null,
      is_active: form.is_active,
      backup_check_enabled: form.backup_check_enabled,
      backup_path: form.backup_path.trim() || null,
      backup_host: form.backup_host.trim() || null,
      backup_use_auth: form.backup_use_auth,
      backup_user: form.backup_use_auth ? form.backup_user.trim() || null : null,
      backup_password: form.backup_use_auth ? form.backup_password || null : null,
      backup_clear_password: form.backup_clear_password,
      backup_check_yesterday: form.backup_check_yesterday,
    }
    let updated = await stores.update(id, payload)

    if (form.clear_password) {
      updated = await stores.setCredentials(id, { clear_password: true })
    } else if (form.password) {
      updated = await stores.setCredentials(id, { password: form.password })
    }

    emit('saved', updated, false)
    close()
  } catch (error) {
    fail(error, 'Не удалось сохранить магазин')
  } finally {
    isSaving.value = false
  }
}
</script>

<template>
  <Dialog
    :visible="visible"
    :header="dialogTitle"
    modal
    :style="{ width: '720px' }"
    data-test="store-form-dialog"
    @update:visible="emit('update:visible', $event)"
  >
    <Message v-if="formError" severity="error" :closable="false" class="mb-3" data-test="form-error">
      {{ formError }}
    </Message>

    <div class="grid gap-3 md:grid-cols-2">
      <label class="space-y-1">
        <span class="magaz-muted text-xs">Имя магазина *</span>
        <InputText v-model="form.name" class="w-full" :pt="{ root: { 'data-test': 'form-name' } }" />
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
          Пароль 1С {{ isEdit ? '(пусто — не менять)' : '' }}
        </span>
        <InputText
          v-model="form.password"
          type="password"
          class="w-full"
          :disabled="form.clear_password"
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
      <label class="space-y-1 md:col-span-2">
        <span class="magaz-muted text-xs">Имя расширения 1С (для слияния)</span>
        <InputText
          v-model="form.extension_name"
          class="w-full"
          placeholder="БГ_Торговоя"
          :pt="{ root: { 'data-test': 'form-extension-name' } }"
        />
      </label>

      <template v-if="isEdit">
        <label class="magaz-muted flex items-center gap-2 text-sm">
          <input
            v-model="form.is_active"
            type="checkbox"
            data-test="form-active"
            :disabled="form.clear_password"
          />
          магазин активен (мониторинг и операции)
        </label>
        <label class="magaz-muted flex items-center gap-2 text-sm">
          <input v-model="form.clear_password" type="checkbox" data-test="form-clear-password" />
          очистить пароль 1С
        </label>
      </template>
    </div>

    <!-- Д-21: проверка бэкапа -->
    <div class="mt-4 rounded-xl border border-slate-200 bg-slate-50 p-4" data-test="backup-block">
      <label class="flex items-center gap-2 text-sm font-medium">
        <input v-model="form.backup_check_enabled" type="checkbox" data-test="backup-enable" />
        проверять наличие бэкапа
      </label>

      <div v-if="form.backup_check_enabled" class="mt-3 grid gap-3 md:grid-cols-2">
        <label class="space-y-1 md:col-span-2">
          <span class="magaz-muted text-xs">Путь к бэкапу (локальный C:\… или сетевой \\…)</span>
          <InputText
            v-model="form.backup_path"
            class="w-full"
            placeholder="\\server\backup\Магазин\…"
            :pt="{ root: { 'data-test': 'backup-path' } }"
          />
        </label>
        <label class="space-y-1">
          <span class="magaz-muted text-xs">Имя машины SMB</span>
          <InputText
            v-model="form.backup_host"
            class="w-full"
            placeholder="ilya"
            :pt="{ root: { 'data-test': 'backup-host' } }"
          />
        </label>
        <label class="flex items-center gap-2 text-sm">
          <select
            v-model="form.backup_check_yesterday"
            class="rounded-lg border border-slate-300 px-3 py-2 text-sm"
            data-test="backup-yesterday"
          >
            <option :value="false">за сегодня</option>
            <option :value="true">за вчера</option>
          </select>
          <span class="magaz-muted text-xs">актуальность бэкапа</span>
        </label>
        <label class="flex items-center gap-2 text-sm">
          <input v-model="form.backup_use_auth" type="checkbox" data-test="backup-auth" />
          отдельная авторизация (не гость)
        </label>

        <template v-if="form.backup_use_auth">
          <label class="space-y-1">
            <span class="magaz-muted text-xs">Логин SMB</span>
            <InputText
              v-model="form.backup_user"
              class="w-full"
              :pt="{ root: { 'data-test': 'backup-user' } }"
            />
          </label>
          <label class="space-y-1">
            <span class="magaz-muted text-xs">Пароль SMB</span>
            <InputText
              v-model="form.backup_password"
              type="password"
              class="w-full"
              :disabled="form.backup_clear_password"
              :pt="{ root: { 'data-test': 'backup-password' } }"
            />
          </label>
          <label v-if="isEdit" class="magaz-muted flex items-center gap-2 text-sm">
            <input v-model="form.backup_clear_password" type="checkbox" data-test="backup-clear-password" />
            очистить пароль SMB
          </label>
        </template>
      </div>
      <p class="magaz-muted mt-2 text-xs">
        Проверка раз в день: если бэкап за {{ form.backup_check_yesterday ? 'вчера' : 'сегодня' }} найден —
        стоп; иначе до 5 попыток с интервалом 3 мин (Backup Monitor).
      </p>
    </div>


    <p class="magaz-muted mt-4 text-xs">
      Пароль 1С хранится на сервере в шифрованном виде и доставляется агенту отдельно (П-1): в
      HTML, логах, аудите и в `agent-config.json` его нет — виден только признак
      {{ passwordSet ? '«задан»' : '«не задан»' }}.
    </p>
    <p v-if="isEdit" class="magaz-muted mt-2 text-xs">
      Имя и IP уникальны только среди активных магазинов: отключённый магазин их не занимает,
      поэтому кассу можно завести заново (§5.2.2). Включить обратно магазин, чьи имя или IP занял
      активный, нельзя — сервер вернёт ошибку.
    </p>

    <template #footer>
      <Button label="Отмена" text size="small" @click="close" />
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
</template>
