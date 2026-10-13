<script setup lang="ts">
/**
 * Корневой компонент: страница входа показывается самостоятельно, все
 * остальные — внутри оболочки с меню (`AppShell`).
 *
 * Пока сессия ещё неизвестна (guard проверяет cookie), оболочку не монтируем:
 * иначе она успевает запустить polling к защищённым эндпоинтам и в консоли
 * появляются «пустые» 401.
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import AppShell from '@/components/AppShell.vue'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const auth = useAuthStore()

const isPublicRoute = computed(() => route.meta.requiresAuth === false)
const isSessionKnown = computed(() => auth.isInitialized)
</script>

<template>
  <RouterView v-if="isPublicRoute" />
  <AppShell v-else-if="isSessionKnown && auth.isAuthenticated" />
  <div v-else class="flex min-h-screen items-center justify-center">
    <span class="magaz-muted text-sm">Проверяем сессию…</span>
  </div>
</template>

