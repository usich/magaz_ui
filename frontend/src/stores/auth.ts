/** Стор аутентификации администратора (§4.2, §5.2.1). */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError } from '@/api/client'
import { authApi } from '@/api/endpoints'
import type { AdminUser } from '@/api/types'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<AdminUser | null>(null)
  const isLoading = ref(false)
  const isInitialized = ref(false)
  const errorMessage = ref('')

  const isAuthenticated = computed(() => user.value !== null)

  /** Вход: при успехе заполняем профиль, при 401 — понятный текст ошибки. */
  async function login(username: string, password: string): Promise<boolean> {
    isLoading.value = true
    errorMessage.value = ''
    try {
      user.value = await authApi.login({ username, password })
      isInitialized.value = true
      return true
    } catch (error) {
      user.value = null
      errorMessage.value =
        error instanceof ApiError && error.isUnauthorized
          ? 'Неверный логин или пароль'
          : error instanceof ApiError
            ? error.message
            : 'Не удалось войти в дашборд'
      return false
    } finally {
      isLoading.value = false
    }
  }

  /** Проверка текущей сессии по cookie (используется guard-ом роутера). */
  async function fetchCurrentUser(): Promise<AdminUser | null> {
    if (isInitialized.value) return user.value

    isLoading.value = true
    try {
      user.value = await authApi.me()
    } catch {
      user.value = null
    } finally {
      isLoading.value = false
      isInitialized.value = true
    }
    return user.value
  }

  async function logout(): Promise<void> {
    try {
      await authApi.logout()
    } catch {
      // Сессия могла уже истечь или сервер недоступен: состояние всё равно
      // сбрасываем, иначе пользователь «застрянет» в интерфейсе (см. §8.1)
    } finally {
      user.value = null
      isInitialized.value = true
      errorMessage.value = ''
    }
  }

  /** Сброс без обращения к серверу (например, при 401 из любого запроса). */
  function reset(): void {
    user.value = null
    isInitialized.value = false
    errorMessage.value = ''
  }

  return {
    user,
    isLoading,
    isInitialized,
    errorMessage,
    isAuthenticated,
    login,
    logout,
    fetchCurrentUser,
    reset,
  }
})
