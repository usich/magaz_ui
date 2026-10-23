/**
 * Стор локальных баз 1С (Д-20).
 *
 * Вкладка «Локальные базы» страницы «Обновление 1С» работает с одним сторем:
 * список грузится при открытии вкладки и перечитывается после каждой операции.
 * Группировка в админке идёт по полю `group` — стор отдаёт отсортированный по
 * группе список и перечень уникальных групп для секций.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError } from '@/api/client'
import { localBasesApi } from '@/api/endpoints'
import type { LocalBaseCreatePayload, LocalBaseItem, LocalBaseUpdatePayload } from '@/api/types'

export const useLocalBasesStore = defineStore('localBases', () => {
  const items = ref<LocalBaseItem[]>([])
  const isLoading = ref(false)
  const errorMessage = ref('')

  /** Базы сгруппированы по `group` (уже отсортированы сервером: group, name). */
  const groups = computed(() => Array.from(new Set(items.value.map((item) => item.group))))

  function byId(id: number): LocalBaseItem | null {
    return items.value.find((item) => item.id === id) ?? null
  }

  function upsert(base: LocalBaseItem): void {
    const index = items.value.findIndex((item) => item.id === base.id)
    if (index === -1) {
      items.value = [...items.value, base]
      return
    }
    items.value = items.value.map((item) => (item.id === base.id ? base : item))
  }

  async function load(): Promise<void> {
    isLoading.value = true
    try {
      items.value = await localBasesApi.list()
      errorMessage.value = ''
    } catch (error) {
      errorMessage.value =
        error instanceof ApiError ? error.message : 'Не удалось получить список локальных баз'
    } finally {
      isLoading.value = false
    }
  }

  async function create(payload: LocalBaseCreatePayload): Promise<LocalBaseItem> {
    const created = await localBasesApi.create(payload)
    await load()
    return created
  }

  async function update(id: number, payload: LocalBaseUpdatePayload): Promise<LocalBaseItem> {
    const updated = await localBasesApi.update(id, payload)
    upsert(updated)
    return updated
  }

  /** Мягкое удаление: `is_active=false` — база уходит из списка. */
  async function remove(id: number): Promise<LocalBaseItem> {
    const updated = await localBasesApi.remove(id)
    items.value = items.value.filter((item) => item.id !== id)
    return updated
  }

  async function setCredentials(
    id: number,
    payload: { user_name_1c?: string; password?: string; clear_password?: boolean },
  ): Promise<LocalBaseItem> {
    const updated = await localBasesApi.setCredentials(id, payload)
    upsert(updated)
    return updated
  }

  return {
    items,
    isLoading,
    errorMessage,
    groups,
    byId,
    load,
    create,
    update,
    remove,
    setCredentials,
  }
})
