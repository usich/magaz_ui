<script setup lang="ts">
/**
 * Форма локальной базы 1С (Д-20): «Добавить базу» и «Изменить базу».
 *
 * Поля — реквизиты подключения (версия 1С, кластер, имя БД, логин/пароль 1С),
 * путь к базе и имя расширения для будущего обновления, плюс поле группировки
 * `group`. Пароль 1С наружу не уходит (П-1): в правке — только «задать/заменить»
 * или «очистить», как у магазина.
 */
import { computed, reactive, ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import { useToast } from 'primevue/usetoast'

import { ApiError } from '@/api/client'
import type { LocalBaseCreatePayload, LocalBaseItem, LocalBaseUpdatePayload } from '@/api/types'
import { useLocalBasesStore } from '@/stores/local_bases'

const props = withDefaults(
  defineProps<{
    visible: boolean
    /** `null` — создание, иначе — правка переданной базы */
    base?: LocalBaseItem | null
    /** Источник для «Скопировать»: поля заполняются, но имя пустое и это создание */
    copyFrom?: LocalBaseItem | null
  }>(),
  { base: null, copyFrom: null },
)

const emit = defineEmits<{
  'update:visible': [value: boolean]
  saved: [base: LocalBaseItem, created: boolean]
}>()

interface LocalBaseForm {
  name: string
  version_1c: string
  cluster_name: string
  database_name: string
  user_name_1c: string
  base_path: string
  extension_name: string
  group: string
  password: string
  comment: string
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

function emptyForm(): LocalBaseForm {
  return {
    name: '',
    version_1c: '',
    cluster_name: '',
    database_name: '',
    user_name_1c: 'Администратор',
    base_path: '',
    extension_name: '',
    group: '',
    password: '',
    comment: '',
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
const store = useLocalBasesStore()

const form = reactive<LocalBaseForm>(emptyForm())
const isSaving = ref(false)
const formError = ref('')

const isEdit = computed(() => props.base !== null)
const dialogTitle = computed(() => (isEdit.value ? 'Изменить базу 1С' : 'Новая локальная база 1С'))
const passwordSet = computed(() => Boolean(props.base?.password_set))
const isFormValid = computed(
  () =>
    Boolean(form.name.trim()) &&
    Boolean(form.version_1c.trim()) &&
    Boolean(form.cluster_name.trim()) &&
    Boolean(form.database_name.trim()) &&
    Boolean(form.user_name_1c.trim()),
)

function reset(): void {
  const source = props.copyFrom ?? props.base
  Object.assign(form, emptyForm(), {
    name: props.copyFrom ? '' : (source?.name ?? ''),
    version_1c: source?.version_1c ?? '',
    cluster_name: source?.cluster_name ?? '',
    database_name: source?.database_name ?? '',
    user_name_1c: source?.user_name_1c ?? 'Администратор',
    base_path: source?.base_path ?? '',
    extension_name: source?.extension_name ?? '',
    group: source?.group ?? '',
    comment: source?.comment ?? '',
    is_active: source?.is_active ?? true,
    backup_check_enabled: source?.backup_check_enabled ?? false,
    backup_path: source?.backup_path ?? '',
    backup_host: source?.backup_host ?? '',
    backup_use_auth: source?.backup_use_auth ?? false,
    backup_user: source?.backup_user ?? '',
    backup_check_yesterday: source?.backup_check_yesterday ?? false,
  })
  formError.value = ''
}

watch(
  () => props.visible,
  (open) => {
    if (open) reset()
  },
)

function close(): void {
  emit('update:visible', false)
}

function notifyError(error: unknown): void {
  const message =
    error instanceof ApiError && error.messages.length > 0
      ? error.messages.join('; ')
      : error instanceof ApiError
        ? error.message
        : 'Не удалось сохранить базу'
  formError.value = message
  toast.add({ severity: 'error', summary: 'Операция не выполнена', detail: message, life: 6000 })
}

async function submit(): Promise<void> {
  isSaving.value = true
  formError.value = ''
  try {
    if (isEdit.value && props.base) {
      const payload: LocalBaseUpdatePayload = {
        name: form.name.trim(),
        version_1c: form.version_1c.trim(),
        cluster_name: form.cluster_name.trim(),
        database_name: form.database_name.trim(),
        user_name_1c: form.user_name_1c.trim(),
        base_path: form.base_path.trim() || null,
        extension_name: form.extension_name.trim() || null,
        group: form.group.trim(),
        comment: form.comment.trim() || null,
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
      const updated = await store.update(props.base.id, payload)
      if (form.clear_password || form.password) {
        await store.setCredentials(props.base.id, {
          ...(form.password ? { password: form.password } : {}),
          clear_password: form.clear_password,
        })
      }
      emit('saved', updated, false)
      toast.add({ severity: 'success', summary: 'База сохранена', detail: updated.name, life: 4000 })
      close()
      return
    }

    const payload: LocalBaseCreatePayload = {
      name: form.name.trim(),
      version_1c: form.version_1c.trim(),
      cluster_name: form.cluster_name.trim(),
      database_name: form.database_name.trim(),
      user_name_1c: form.user_name_1c.trim(),
      base_path: form.base_path.trim() || null,
      extension_name: form.extension_name.trim() || null,
      group: form.group.trim(),
      password: form.password || null,
      comment: form.comment.trim() || null,
      backup_check_enabled: form.backup_check_enabled,
      backup_path: form.backup_path.trim() || null,
      backup_host: form.backup_host.trim() || null,
      backup_use_auth: form.backup_use_auth,
      backup_user: form.backup_use_auth ? form.backup_user.trim() || null : null,
      backup_password: form.backup_use_auth ? form.backup_password || null : null,
      backup_clear_password: form.backup_clear_password,
      backup_check_yesterday: form.backup_check_yesterday,
    }
    const created = await store.create(payload)
    emit('saved', created, true)
    toast.add({ severity: 'success', summary: 'База создана', detail: created.name, life: 4000 })
    close()
  } catch (error) {
    notifyError(error)
  } finally {
    isSaving.value = false
  }
}
</script>

<template>
  <Dialog
    :visible="props.visible"
    :header="dialogTitle"
    modal
    :style="{ width: '640px' }"
    data-test="local-base-dialog"
    @update:visible="(v: boolean) => emit('update:visible', v)"
  >
    <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
      <label class="space-y-1">
        <span class="magaz-muted text-xs">Название базы *</span>
        <InputText v-model="form.name" class="w-full" data-test="lb-name" />
      </label>
      <label class="space-y-1">
        <span class="magaz-muted text-xs">Версия 1С *</span>
        <InputText v-model="form.version_1c" class="w-full" placeholder="8.3.24.1548" data-test="lb-version" />
      </label>
      <label class="space-y-1">
        <span class="magaz-muted text-xs">Кластер (сервер 1С) *</span>
        <InputText v-model="form.cluster_name" class="w-full" data-test="lb-cluster" />
      </label>
      <label class="space-y-1">
        <span class="magaz-muted text-xs">Имя базы 1С *</span>
        <InputText v-model="form.database_name" class="w-full" data-test="lb-database" />
      </label>
      <label class="space-y-1 md:col-span-2">
        <span class="magaz-muted text-xs">Путь к базе (папка/файл)</span>
        <InputText v-model="form.base_path" class="w-full" placeholder="C:\1C\Бухгалтерия" data-test="lb-base-path" />
      </label>
      <label class="space-y-1">
        <span class="magaz-muted text-xs">Пользователь 1С *</span>
        <InputText v-model="form.user_name_1c" class="w-full" data-test="lb-user" />
      </label>
      <label class="space-y-1">
        <span class="magaz-muted text-xs">Пароль 1С {{ isEdit ? '(пусто — не менять)' : '' }}</span>
        <InputText
          v-model="form.password"
          type="password"
          class="w-full"
          :disabled="form.clear_password"
          data-test="lb-password"
        />
      </label>
      <label class="space-y-1">
        <span class="magaz-muted text-xs">Группа (для группировки)</span>
        <InputText v-model="form.group" class="w-full" placeholder="Бухгалтерия" data-test="lb-group" />
      </label>
      <label class="space-y-1">
        <span class="magaz-muted text-xs">Имя расширения 1С</span>
        <InputText v-model="form.extension_name" class="w-full" placeholder="БГ_Бухгалтерия" data-test="lb-extension" />
      </label>
      <label class="space-y-1 md:col-span-2">
        <span class="magaz-muted text-xs">Комментарий</span>
        <InputText v-model="form.comment" class="w-full" data-test="lb-comment" />
      </label>

      <template v-if="isEdit">
        <label class="magaz-muted flex items-center gap-2 text-sm md:col-span-2">
          <input v-model="form.is_active" type="checkbox" data-test="lb-active" :disabled="form.clear_password" />
          база активна
        </label>
        <label class="magaz-muted flex items-center gap-2 text-sm md:col-span-2">
          <input v-model="form.clear_password" type="checkbox" data-test="lb-clear-password" />
          очистить пароль 1С
        </label>
      </template>
    </div>

    <!-- Д-21: проверка бэкапа -->
    <div class="mt-4 rounded-xl border border-slate-200 bg-slate-50 p-4" data-test="lb-backup-block">
      <label class="flex items-center gap-2 text-sm font-medium">
        <input v-model="form.backup_check_enabled" type="checkbox" data-test="lb-backup-enable" />
        проверять наличие бэкапа
      </label>

      <div v-if="form.backup_check_enabled" class="mt-3 grid gap-3 md:grid-cols-2">
        <label class="space-y-1 md:col-span-2">
          <span class="magaz-muted text-xs">Путь к бэкапу (локальный C:\… или сетевой \\…)</span>
          <InputText v-model="form.backup_path" class="w-full" placeholder="\\server\backup\…" data-test="lb-backup-path" />
        </label>
        <label class="space-y-1">
          <span class="magaz-muted text-xs">Имя машины SMB</span>
          <InputText v-model="form.backup_host" class="w-full" placeholder="ilya" data-test="lb-backup-host" />
        </label>
        <label class="flex items-center gap-2 text-sm">
          <select
            v-model="form.backup_check_yesterday"
            class="rounded-lg border border-slate-300 px-3 py-2 text-sm"
            data-test="lb-backup-yesterday"
          >
            <option :value="false">за сегодня</option>
            <option :value="true">за вчера</option>
          </select>
          <span class="magaz-muted text-xs">актуальность бэкапа</span>
        </label>
        <label class="flex items-center gap-2 text-sm">
          <input v-model="form.backup_use_auth" type="checkbox" data-test="lb-backup-auth" />
          отдельная авторизация (не гость)
        </label>

        <template v-if="form.backup_use_auth">
          <label class="space-y-1">
            <span class="magaz-muted text-xs">Логин SMB</span>
            <InputText v-model="form.backup_user" class="w-full" data-test="lb-backup-user" />
          </label>
          <label class="space-y-1">
            <span class="magaz-muted text-xs">Пароль SMB</span>
            <InputText
              v-model="form.backup_password"
              type="password"
              class="w-full"
              :disabled="form.backup_clear_password"
              data-test="lb-backup-password"
            />
          </label>
          <label v-if="isEdit" class="magaz-muted flex items-center gap-2 text-sm">
            <input v-model="form.backup_clear_password" type="checkbox" data-test="lb-backup-clear-password" />
            очистить пароль SMB
          </label>
        </template>
      </div>
      <p class="magaz-muted mt-2 text-xs">
        Проверка раз в день: если бэкап за {{ form.backup_check_yesterday ? 'вчера' : 'сегодня' }} найден —
        стоп; иначе до 5 попыток с интервалом 3 мин.
      </p>
    </div>


    <p v-if="formError" class="mt-3 rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700" data-test="lb-error">
      {{ formError }}
    </p>
    <p class="magaz-muted mt-4 text-xs">
      Пароль 1С хранится на сервере в шифрованном виде и в ответы API не попадает — виден только признак
      {{ passwordSet ? '«задан»' : '«не задан»' }}. Базы с одинаковым значением «Группы» собираются в одну секцию.
    </p>

    <template #footer>
      <Button label="Отмена" text size="small" @click="close" />
      <Button
        label="Сохранить"
        icon="pi pi-save"
        size="small"
        :loading="isSaving"
        :disabled="!isFormValid"
        data-test="save-local-base"
        @click="submit"
      />
    </template>
  </Dialog>
</template>

