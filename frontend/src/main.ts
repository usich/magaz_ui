/**
 * Точка входа SPA (§6): Pinia → router → PrimeVue (тема Aura) → монтирование.
 *
 * Обработчик `401` из любого запроса сбрасывает сессию и уводит на `/login`
 * (см. `src/api/client.ts`) — так «протухшая» cookie не оставляет пользователя
 * на пустом экране.
 */

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import PrimeVue from 'primevue/config'
import ToastService from 'primevue/toastservice'
import Aura from '@primeuix/themes/aura'

import 'primeicons/primeicons.css'
import './style.css'

import App from './App.vue'
import { registerUnauthorizedHandler } from './api/client'
import { createAppRouter } from './router'
import { useAuthStore } from './stores/auth'

const app = createApp(App)
const pinia = createPinia()
app.use(pinia)

const router = createAppRouter(pinia)
app.use(router)

app.use(PrimeVue, {
  ripple: true,
  theme: {
    preset: Aura,
    options: {
      // Класс тёмной темы не используем: дашборд — светлый (см. тона в style.css)
      darkModeSelector: '.app-dark',
      cssLayer: false,
    },
  },
})
app.use(ToastService)

const auth = useAuthStore(pinia)
registerUnauthorizedHandler(() => {
  auth.reset()
  if (router.currentRoute.value.name !== 'login') {
    void router.push({ name: 'login', query: { redirect: router.currentRoute.value.fullPath } })
  }
})

app.mount('#app')
