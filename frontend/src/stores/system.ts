/**
 * Стор состояния сервера (§11.1): версия, ревизия схемы, собрана ли SPA, счётчики.
 *
 * Данные обновляются polling-ом (каждые ~10 с) без перезагрузки страницы —
 * так же, как это делают страницы магазинов и устройств на своих этапах.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError } from '@/api/client'
import { systemApi } from '@/api/endpoints'
import type { SystemInfo } from '@/api/types'

export const POLL_INTERVAL_MS = 10_000

export const useSystemStore = defineStore('system', () => {
  const info = ref<SystemInfo | null>(null)
  const isLoading = ref(false)
  const errorMessage = ref('')
  const lastLoadedAt = ref<Date | null>(null)

  const isSpaBuilt = computed(() => info.value?.spa.built ?? false)
  const counts = computed(() => info.value?.counts ?? null)

  async function load(): Promise<void> {
    isLoading.value = true
    try {
      info.value = await systemApi.info()
      lastLoadedAt.value = new Date()
      errorMessage.value = ''
    } catch (error) {
      errorMessage.value =
        error instanceof ApiError ? error.message : 'Не удалось получить состояние сервера'
    } finally {
      isLoading.value = false
    }
  }

  let timer: ReturnType<typeof setInterval> | null = null

  /** Запустить автообновление (идемпотентно). */
  function startPolling(intervalMs: number = POLL_INTERVAL_MS): void {
    stopPolling()
    void load()
    timer = setInterval(() => {
      void load()
    }, intervalMs)
  }

  function stopPolling(): void {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }

  return {
    info,
    isLoading,
    errorMessage,
    lastLoadedAt,
    isSpaBuilt,
    counts,
    load,
    startPolling,
    stopPolling,
  }
})
