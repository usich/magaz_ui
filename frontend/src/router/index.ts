/**
 * Маршруты SPA (§5.2.1) и guard сессии администратора.
 *
 * Роутер создаётся фабрикой `createAppRouter(pinia)` — так его можно поднять в
 * тестах с памятью вместо истории браузера, не подменяя глобальное состояние.
 */

import type { Pinia } from 'pinia'
import {
  createRouter,
  createWebHistory,
  type RouteRecordRaw,
  type Router,
  type RouterHistory,
} from 'vue-router'

import { useAuthStore } from '@/stores/auth'

declare module 'vue-router' {
  interface RouteMeta {
    /** `false` — страница доступна без входа (только `/login`) */
    requiresAuth?: boolean
    title?: string
    /** Этап спецификации, на котором раздел наполняется данными (§12) */
    stage?: string
  }
}

export const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { requiresAuth: false, title: 'Вход' },
  },
  {
    path: '/',
    name: 'dashboard',
    component: () => import('@/views/DashboardView.vue'),
    meta: { title: 'Главная' },
  },
  {
    path: '/stores',
    name: 'stores',
    component: () => import('@/views/StoresView.vue'),
    meta: { title: 'Магазины', stage: 'Э2' },
  },
  {
    path: '/stores/:id(\\d+)',
    name: 'store-detail',
    component: () => import('@/views/StoreDetailView.vue'),
    props: true,
    meta: { title: 'Карточка магазина', stage: 'Э2' },
  },
  {
    path: '/files',
    name: 'files',
    component: () => import('@/views/FilesView.vue'),
    meta: { title: 'Файлы обновления', stage: 'Э4' },
  },
  {
    path: '/devices',
    name: 'devices',
    component: () => import('@/views/DevicesView.vue'),
    meta: { title: 'Оборудование подсети', stage: 'Э4Б' },
  },
  {
    path: '/notifications',
    name: 'notifications',
    component: () => import('@/views/NotificationsView.vue'),
    meta: { title: 'Уведомления', stage: 'Э5' },
  },
  {
    path: '/notifications/:id(\\d+)',
    name: 'notification-detail',
    component: () => import('@/views/NotificationDetailView.vue'),
    props: true,
    meta: { title: 'Отчёт по уведомлению', stage: 'Э5' },
  },
  {
    path: '/jobs',
    name: 'jobs',
    component: () => import('@/views/JobsView.vue'),
    meta: { title: 'Задачи', stage: 'Э4' },
  },
  {
    path: '/audit',
    name: 'audit',
    component: () => import('@/views/AuditView.vue'),
    meta: { title: 'Аудит' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/views/SettingsView.vue'),
    meta: { title: 'Настройки' },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { requiresAuth: false, title: 'Страница не найдена' },
  },
]

export function createAppRouter(pinia: Pinia, history?: RouterHistory): Router {
  const router = createRouter({
    history: history ?? createWebHistory(),
    routes,
  })

  router.beforeEach(async (to) => {
    const auth = useAuthStore(pinia)

    if (to.meta.requiresAuth === false) {
      // Уже вошедшего администратора со страницы входа уводим на главную
      if (to.name === 'login' && (await auth.fetchCurrentUser())) {
        return { name: 'dashboard' }
      }
      return true
    }

    const user = await auth.fetchCurrentUser()
    if (!user) {
      return { name: 'login', query: to.fullPath === '/' ? {} : { redirect: to.fullPath } }
    }
    return true
  })

  router.afterEach((to) => {
    document.title = to.meta.title ? `${to.meta.title} — Magaz UI` : 'Magaz UI'
  })

  return router
}
