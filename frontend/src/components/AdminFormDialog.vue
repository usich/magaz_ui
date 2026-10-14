<script setup lang="ts">
/**
 * Форма администратора (§11.6, Д-8, Э6): создание учётной записи и смена пароля.
 *
 * Одна форма на два действия: в режиме `create` вводятся логин и пароль, в
 * режиме `password` логин показан как есть (менять его нельзя — адрес учётной
 * записи в аудите остаётся прежним, а для нового логина заводится новая запись).
 * Пароль нигде не сохраняется: уходит телом запроса в стор и сразу забывается.
 */
import { computed, reactive, ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'

import type { AdminUser } from '@/api/types'
import { ADMIN_PASSWORD_MIN_LENGTH, useAdminsStore } from '@/stores/admins'

const props = withDefaults(
  defineProps<{
    visible: boolean
    mode?: 'create' | 'password'
    admin?: AdminUser | null
  }>(),
  { mode: 'create', admin: null },
)

const emit = defineEmits<{
  'update:visible': [value: boolean]
  saved: [admin: AdminUser]
}>()

const admins = useAdminsStore()

const form = reactive({ username: '', password: '' })
const formError = ref('')

const isCreate = computed(() => props.mode === 'create')
const header = computed(() =>
  isCreate.value ? 'Новый администратор' : `Смена пароля: ${props.admin?.username ?? ''}`,
)

/** Пароль проверяется до запроса: сервер ответит тем же текстом, но позже. */
const passwordTooShort = computed(
  () => form.password.length > 0 && form.password.length < ADMIN_PASSWORD_MIN_LENGTH,
)
const canSubmit = computed(
  () =>
    form.password.length >= ADMIN_PASSWORD_MIN_LENGTH &&
    (!isCreate.value || form.username.trim().length > 0),
)

function reset(): void {
  form.username = ''
  form.password = ''
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

async function submit(): Promise<void> {
  if (!canSubmit.value) {
    formError.value = isCreate.value
      ? `Заполните логин и пароль длиной не менее ${ADMIN_PASSWORD_MIN_LENGTH} символов`
      : `Пароль должен быть не короче ${ADMIN_PASSWORD_MIN_LENGTH} символов`
    return
  }

  const saved = isCreate.value
    ? await admins.create({ username: form.username.trim(), password: form.password })
    : await admins.update(props.admin?.id ?? 0, { password: form.password })

  if (!saved) {
    formError.value = admins.errorMessage || 'Изменения не сохранены'
    return
  }
  emit('saved', saved)
  close()
}
</script>

<template>
  <Dialog
    :visible="visible"
    :header="header"
    modal
    :style="{ width: '26rem' }"
    @update:visible="emit('update:visible', $event)"
  >
    <div class="space-y-3">
      <Message v-if="formError" severity="error" :closable="false" data-test="admin-form-error">
        {{ formError }}
      </Message>

      <div v-if="isCreate" class="space-y-1">
        <label class="magaz-muted text-sm" for="admin-form-username">Логин</label>
        <InputText
          id="admin-form-username"
          v-model="form.username"
          class="w-full"
          :pt="{ root: { 'data-test': 'admin-form-username' } }"
        />
      </div>
      <div v-else class="magaz-muted text-sm">
        Логин: <span class="magaz-mono">{{ admin?.username }}</span>
      </div>

      <div class="space-y-1">
        <label class="magaz-muted text-sm" for="admin-form-password">
          {{ isCreate ? 'Пароль' : 'Новый пароль' }}
        </label>
        <InputText
          id="admin-form-password"
          v-model="form.password"
          type="password"
          class="w-full"
          :pt="{ root: { 'data-test': 'admin-form-password' } }"
        />
        <p
          v-if="passwordTooShort"
          class="text-xs text-amber-600"
          data-test="admin-form-hint"
        >
          Не короче {{ ADMIN_PASSWORD_MIN_LENGTH }} символов
        </p>
      </div>
    </div>

    <template #footer>
      <Button label="Отмена" text size="small" @click="close" />
      <Button
        label="Сохранить"
        size="small"
        icon="pi pi-save"
        :disabled="!canSubmit"
        data-test="admin-form-submit"
        @click="submit"
      />
    </template>
  </Dialog>
</template>
