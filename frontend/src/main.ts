/**
 * Точка входа SPA (§6): Pinia → router → PrimeVue (тема Aura) → тема оформления
 * → монтирование.
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
import { useThemeStore } from './stores/theme'

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
      // Класс `.app-dark` ставит стор темы (`stores/theme.ts`): светлая тема,
      // тёмная и «как в системе» переключаются в шапке (§11.1, Э8)
      darkModeSelector: '.app-dark',
      cssLayer: false,
    },
  },
})
app.use(ToastService)

// Тему применяем до монтирования: иначе после перезагрузки страницы успевает
// мелькнуть светлый фон, а PrimeVue уже посчитал тёмные токены
useThemeStore(pinia).initialize()

const auth = useAuthStore(pinia)
registerUnauthorizedHandler(() => {
  auth.reset()
  if (router.currentRoute.value.name !== 'login') {
    void router.push({ name: 'login', query: { redirect: router.currentRoute.value.fullPath } })
  }
})

app.mount('#app')
