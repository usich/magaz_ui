<script setup lang="ts">
/**
 * Настройки (§4.9, §11.6). Работает уже в Э1: значения читаются из БД и
 * меняются без перезапуска сервера — на них опираются мониторинг (Э2),
 * опрос оборудования (Э4Б) и возврат отложенных уведомлений (Э5).
 *
 * Э6 (Д-8) добавляет второй блок — управление администраторами: список,
 * создание, смена пароля, блокировка и удаление. Правила доступа проверяет
 * сервер, поэтому текст отказа (`403` — «свою учётную запись», `409` — «последний
 * действующий») показывается как есть.
 */
import { computed, onMounted, ref } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'

import AdminFormDialog from '@/components/AdminFormDialog.vue'
import { ApiError } from '@/api/client'
import { settingsApi } from '@/api/endpoints'
import type { AdminUser, SettingItem } from '@/api/types'
import { useAdminsStore } from '@/stores/admins'
import { useAuthStore } from '@/stores/auth'

const toast = useToast()
const admins = useAdminsStore()
const auth = useAuthStore()

const items = ref<SettingItem[]>([])
const draft = ref<Record<string, string>>({})
const isLoading = ref(false)
const isSaving = ref(false)
const errorMessage = ref('')

// --- Управление администраторами (Д-8) ---
const isAdminFormVisible = ref(false)
const adminFormMode = ref<'create' | 'password'>('create')
const passwordTarget = ref<AdminUser | null>(null)
const deleteTarget = ref<AdminUser | null>(null)
const adminError = ref('')

const currentAdminId = computed(() => auth.user?.id ?? null)
const hasChanges = computed(() => changedKeys.value.length > 0)

/** «Своё» действие над своей учётной записью сервер отклонит — не показываем. */
function isSelf(admin: AdminUser): boolean {
  return currentAdminId.value !== null && admin.id === currentAdminId.value
}

function formatMoment(value: string | null): string {
  if (!value) return 'не входил'
  const moment = new Date(value)
  if (Number.isNaN(moment.getTime())) return value
  return moment.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' })
}

function openCreateAdmin(): void {
  adminFormMode.value = 'create'
  passwordTarget.value = null
  isAdminFormVisible.value = true
}

function openPasswordForm(admin: AdminUser): void {
  adminFormMode.value = 'password'
  passwordTarget.value = admin
  isAdminFormVisible.value = true
}

function onAdminSaved(admin: AdminUser): void {
  toast.add({
    severity: 'success',
    summary: adminFormMode.value === 'create' ? 'Администратор создан' : 'Пароль изменён',
    detail: admin.username,
    life: 3000,
  })
}

async function toggleAdminActive(admin: AdminUser): Promise<void> {
  adminError.value = ''
  const updated = await admins.setActive(admin.id, !admin.is_active)
  if (!updated) {
    adminError.value = admins.errorMessage
    return
  }
  toast.add({
    severity: updated.is_active ? 'success' : 'warn',
    summary: updated.is_active ? 'Доступ возвращён' : 'Администратор заблокирован',
    detail: updated.username,
    life: 3000,
  })
}

async function confirmDeleteAdmin(): Promise<void> {
  const target = deleteTarget.value
  if (!target) return
  adminError.value = ''
  const deleted = await admins.remove(target.id)
  deleteTarget.value = null
  if (!deleted) {
    adminError.value = admins.errorMessage
    return
  }
  toast.add({
    severity: 'success',
    summary: 'Администратор удалён',
    detail: target.username,
    life: 3000,
  })
}

const changedKeys = computed(() =>
  items.value
    .filter((item) => draft.value[item.key] !== item.value)
    .map((item) => item.key),
)

onMounted(() => {
  void load()
  void admins.load()
})


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

    <!-- Управление администраторами (Д-8, Э6) -->
    <section class="space-y-3" data-test="admins-section">
      <div class="flex items-center justify-between">
        <div>
          <h2 class="text-base font-semibold">Администраторы</h2>
          <p class="magaz-muted text-sm">
            Действующих:
            <strong data-test="admins-active-count">{{ admins.activeCount }}</strong> из
            <span data-test="admins-total-count">{{ admins.items.length }}</span>. Себя и последнего
            действующего администратора заблокировать нельзя.
          </p>
        </div>
        <Button
          label="Добавить администратора"
          icon="pi pi-user-plus"
          size="small"
          data-test="admins-create"
          @click="openCreateAdmin"
        />
      </div>

      <Message v-if="adminError" severity="error" :closable="false" data-test="admins-error">
        {{ adminError }}
      </Message>

      <div class="overflow-hidden rounded-xl border border-slate-200 bg-white">
        <table class="magaz-table">
          <thead>
            <tr>
              <th>Логин</th>
              <th class="w-48">Последний вход</th>
              <th class="w-40">Доступ</th>
              <th class="w-72"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="admin in admins.items" :key="admin.id" :data-test="`admin-row-${admin.id}`">
              <td class="magaz-mono">
                {{ admin.username }}
                <Tag v-if="isSelf(admin)" value="это вы" severity="info" />
              </td>
              <td class="magaz-muted" :data-test="`admin-last-login-${admin.id}`">
                {{ formatMoment(admin.last_login_at) }}
              </td>
              <td>
                <Tag
                  :value="admin.is_active ? 'действует' : 'заблокирован'"
                  :severity="admin.is_active ? 'success' : 'danger'"
                  :data-test="`admin-status-${admin.id}`"
                />
              </td>
              <td class="whitespace-nowrap">
                <Button
                  label="Сменить пароль"
                  size="small"
                  text
                  :data-test="`admin-password-${admin.id}`"
                  @click="openPasswordForm(admin)"
                />
                <Button
                  :label="admin.is_active ? 'Заблокировать' : 'Разблокировать'"
                  size="small"
                  text
                  :data-test="`admin-toggle-${admin.id}`"
                  @click="toggleAdminActive(admin)"
                />
                <Button
                  v-if="!isSelf(admin)"
                  label="Удалить"
                  size="small"
                  text
                  severity="danger"
                  :data-test="`admin-delete-${admin.id}`"
                  @click="deleteTarget = admin"
                />
              </td>
            </tr>
            <tr v-if="!admins.items.length">
              <td colspan="4" class="magaz-muted py-6 text-center" data-test="admins-empty">
                Администраторы не загружены
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <AdminFormDialog
      v-model:visible="isAdminFormVisible"
      :mode="adminFormMode"
      :admin="passwordTarget"
      @saved="onAdminSaved"
    />

    <Dialog
      :visible="deleteTarget !== null"
      header="Удалить администратора?"
      modal
      :style="{ width: '24rem' }"
      data-test="admin-delete-dialog"
      @update:visible="deleteTarget = null"
    >
      <p class="text-sm">
        Учётная запись <strong class="magaz-mono">{{ deleteTarget?.username }}</strong> будет удалена.
        История её действий останется в аудите (§11.6).
      </p>
      <template #footer>
        <Button label="Отмена" text size="small" @click="deleteTarget = null" />
        <Button
          label="Удалить"
          severity="danger"
          size="small"
          data-test="admin-delete-confirm"
          @click="confirmDeleteAdmin"
        />
      </template>
    </Dialog>
  </div>
</template>
