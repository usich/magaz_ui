<script setup lang="ts">
/**
 * Страница входа (§5.2.1). Пароль нигде не сохраняется в браузере: сессия
 * живёт в подписанной cookie, выставленной сервером (§8.1).
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Password from 'primevue/password'

import { systemApi } from '@/api/endpoints'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const username = ref('')
const password = ref('')
const serverStatus = ref<'checking' | 'online' | 'unreachable'>('checking')
const serverVersion = ref('—')

const canSubmit = computed(
  () => username.value.trim().length > 0 && password.value.length > 0 && !auth.isLoading,
)

onMounted(async () => {
  try {
    const health = await systemApi.health()
    serverVersion.value = health.app_version
    serverStatus.value = 'online'
  } catch {
    serverStatus.value = 'unreachable'
  }
})

async function submit(): Promise<void> {
  if (!canSubmit.value) return

  const success = await auth.login(username.value.trim(), password.value)
  if (!success) {
    password.value = ''
    return
  }

  const redirect = route.query.redirect
  await router.push(typeof redirect === 'string' && redirect ? redirect : { name: 'dashboard' })
}
</script>

<template>
  <div class="flex min-h-screen items-center justify-center bg-slate-100 px-4">
    <div class="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-8 shadow-lg">
      <div class="mb-6 flex items-center gap-3">
        <span
          class="flex h-11 w-11 items-center justify-center rounded-xl bg-blue-700 text-lg font-bold text-white"
          >M</span
        >
        <div>
          <h1 class="text-xl font-semibold">Magaz UI</h1>
          <p class="magaz-muted text-xs">Дашборд обновления 1С в магазинах</p>
        </div>
      </div>

      <form class="space-y-4" @submit.prevent="submit">
        <div class="space-y-1">
          <label for="username" class="text-sm font-medium text-slate-700">Логин</label>
          <InputText
            id="username"
            v-model="username"
            class="w-full"
            autocomplete="username"
            data-test="username"
          />
        </div>

        <div class="space-y-1">
          <label for="password" class="text-sm font-medium text-slate-700">Пароль</label>
          <Password
            id="password"
            v-model="password"
            class="w-full"
            input-class="w-full"
            :feedback="false"
            toggle-mask
            autocomplete="current-password"
            data-test="password"
          />
        </div>

        <Message v-if="auth.errorMessage" severity="error" :closable="false" data-test="login-error">
          {{ auth.errorMessage }}
        </Message>

        <Button
          type="submit"
          label="Войти"
          icon="pi pi-sign-in"
          class="w-full"
          :loading="auth.isLoading"
          :disabled="!canSubmit"
          data-test="submit"
        />
      </form>

      <div class="mt-6 flex items-center gap-2 text-xs">
        <i
          :class="[
            'pi pi-circle-fill text-[8px]',
            serverStatus === 'online'
              ? 'text-emerald-500'
              : serverStatus === 'unreachable'
                ? 'text-rose-500'
                : 'text-slate-300',
          ]"
        />
        <span class="magaz-muted">
          <template v-if="serverStatus === 'online'">Сервер отвечает, версия {{ serverVersion }}</template>
          <template v-else-if="serverStatus === 'unreachable'">Сервер дашборда недоступен</template>
          <template v-else>Проверяем связь с сервером…</template>
        </span>
      </div>
    </div>
  </div>
</template>
