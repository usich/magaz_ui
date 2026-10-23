/**
 * Стор бэкапов 1С (Д-21).
 *
 * Держит сводку для блока «Бэкапы» на главной (магазины / локальные базы) и
 * объединённый список объектов с включённой проверкой. Статусы пишет фоновая
 * задача Backup Monitor в поля объектов; этот стор только читает `/backup/*`.
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

import { ApiError } from '@/api/client'
import { backupApi } from '@/api/endpoints'
import type { BackupListItem, BackupStatus } from '@/api/types'

const EMPTY: BackupStatus = {
  stores: { total: 0, checked: 0, ok: 0, missing: 0 },
  local_bases: { total: 0, checked: 0, ok: 0, missing: 0 },
  next_check_iso: null,
}

export const useBackupStore = defineStore('backup', () => {
  const status = ref<BackupStatus>(EMPTY)
  const storeList = ref<BackupListItem[]>([])
  const localBaseList = ref<BackupListItem[]>([])
  const isLoading = ref(false)
  const isChecking = ref(false)
  const errorMessage = ref('')
  const lastLoadedAt = ref<Date | null>(null)

  async function load(): Promise<void> {
    isLoading.value = true
    try {
      const [st, stores, bases] = await Promise.all([
        backupApi.status(),
        backupApi.storeList(),
        backupApi.localBaseList(),
      ])
      status.value = st
      storeList.value = stores
      localBaseList.value = bases
      lastLoadedAt.value = new Date()
      errorMessage.value = ''
    } catch (error) {
      errorMessage.value =
        error instanceof ApiError ? error.message : 'Не удалось получить статус бэкапов'
    } finally {
      isLoading.value = false
    }
  }

  /** Ручная проверка: опросить только не найденные бэкапы, затем перечитать статусы. */
  async function checkMissing(): Promise<number> {
    isChecking.value = true
    try {
      const result = await backupApi.check()
      await load()
      return result.checked
    } finally {
      isChecking.value = false
    }
  }

  return {
    status,
    storeList,
    localBaseList,
    isLoading,
    isChecking,
    errorMessage,
    lastLoadedAt,
    load,
    checkMissing,
  }
})
