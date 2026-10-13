<script setup lang="ts">
/**
 * Оболочка дашборда: боковое меню (маршруты §5.2.1), шапка с версией сервера
 * и блоком администратора. Страница входа отдаётся без оболочки.
 */
import { computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Button from 'primevue/button'
import Toast from 'primevue/toast'

import { useAuthStore } from '@/stores/auth'
import { useSystemStore } from '@/stores/system'

interface NavItem {
  label: string
  route: string
  icon: string
  stage?: string
}

const navItems: NavItem[] = [
  { label: 'Главная', route: 'dashboard', icon: 'pi pi-home' },
  { label: 'Магазины', route: 'stores', icon: 'pi pi-building' },
  { label: 'Файлы', route: 'files', icon: 'pi pi-file', stage: 'Э4' },
  { label: 'Оборудование', route: 'devices', icon: 'pi pi-wifi', stage: 'Э4Б' },
  { label: 'Уведомления', route: 'notifications', icon: 'pi pi-bell', stage: 'Э5' },
  { label: 'Задачи', route: 'jobs', icon: 'pi pi-list', stage: 'Э4' },
  { label: 'Аудит', route: 'audit', icon: 'pi pi-history' },
  { label: 'Настройки', route: 'settings', icon: 'pi pi-cog' },
]

const auth = useAuthStore()
const system = useSystemStore()
const router = useRouter()
const route = useRoute()

const serverVersion = computed(() => system.info?.app_version ?? '—')
const isSpaBuilt = computed(() => system.isSpaBuilt)
const currentTitle = computed(() => route.meta.title ?? 'Magaz UI')

onMounted(() => {
  system.startPolling()
})

onUnmounted(() => {
  system.stopPolling()
})

async function handleLogout(): Promise<void> {
  system.stopPolling()
  await auth.logout()
  await router.push({ name: 'login' })
}
</script>

<template>
  <div class="flex min-h-screen">
    <aside class="flex w-64 shrink-0 flex-col border-r border-slate-200 bg-white">
      <div class="flex items-center gap-3 px-5 py-5">
        <span
          class="flex h-10 w-10 items-center justify-center rounded-xl bg-blue-700 text-lg font-bold text-white"
          >M</span
        >
        <div>
          <p class="text-base font-semibold leading-tight">Magaz UI</p>
          <p class="magaz-muted text-xs">Сервер {{ serverVersion }}</p>
        </div>
      </div>

      <nav class="flex-1 space-y-1 px-3">
        <RouterLink
          v-for="item in navItems"
          :key="item.route"
          :to="{ name: item.route }"
          class="flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-100"
          active-class="bg-blue-50 text-blue-700"
        >
          <i :class="item.icon" class="text-slate-400" />
          <span>{{ item.label }}</span>
          <span
            v-if="item.stage"
            class="magaz-muted ml-auto rounded bg-slate-100 px-1.5 py-0.5 text-[10px]"
            >{{ item.stage }}</span
          >
        </RouterLink>
      </nav>

      <div class="border-t border-slate-200 px-5 py-4">
        <p class="magaz-muted text-xs">Состояние сервера</p>
        <p class="mt-1 flex items-center gap-2 text-sm">
          <i
            :class="['pi pi-circle-fill text-[8px]', system.errorMessage ? 'text-rose-500' : 'text-emerald-500']"
          />
          <span>{{ system.errorMessage ? 'нет связи с API' : 'API отвечает' }}</span>
        </p>
        <p class="magaz-muted mt-1 text-xs">
          SPA: {{ isSpaBuilt ? 'собрана' : 'не собрана' }}
        </p>
      </div>
    </aside>

    <div class="flex min-w-0 flex-1 flex-col">
      <header
        class="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-4"
      >
        <div>
          <h1 class="text-lg font-semibold">{{ currentTitle }}</h1>
          <p class="magaz-muted text-xs">
            Обновление данных: {{ system.lastLoadedAt?.toLocaleTimeString('ru-RU') ?? '—' }}
          </p>
        </div>

        <div class="flex items-center gap-4">
          <span class="text-sm text-slate-600">{{ auth.user?.username ?? '—' }}</span>
          <Button
            label="Выйти"
            icon="pi pi-sign-out"
            severity="secondary"
            size="small"
            outlined
            data-test="logout"
            @click="handleLogout"
          />
        </div>
      </header>

      <main class="flex-1 overflow-x-hidden px-6 py-6">
        <RouterView />
      </main>
    </div>

    <Toast position="bottom-right" />
  </div>
</template>
