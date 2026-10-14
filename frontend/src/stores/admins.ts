/**
 * Стор администраторов дашборда (§11.6, Д-8, Э6).
 *
 * Одна страница — `/settings`: список учётных записей, создание, смена пароля,
 * блокировка и удаление. Правила доступа («нельзя себя», «нельзя последнего
 * действующего») проверяет сервер — стор эти сообщения не переписывает: текст
 * из `403`/`409` уходит в интерфейс как есть, чтобы администратор понимал
 * причину отказа.
 *
 * Пароль никогда не хранится в сторе: он уходит телом запроса и сразу
 * забывается — в состоянии остаются только данные из ответа API.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError } from '@/api/client'
import { adminsApi } from '@/api/endpoints'
import type { AdminCreatePayload, AdminUpdatePayload, AdminUser } from '@/api/types'

/** Минимальная длина пароля — подсказка в форме (сервер проверяет по-своему). */
export const ADMIN_PASSWORD_MIN_LENGTH = 8

export const useAdminsStore = defineStore('admins', () => {
  const items = ref<AdminUser[]>([])
  const isLoading = ref(false)
  const errorMessage = ref('')
  const lastLoadedAt = ref<Date | null>(null)

  /** Счётчики для подписи над таблицей: «действующих N из M». */
  const activeCount = computed(() => items.value.filter((item) => item.is_active).length)

  /** Текущий администратор в списке помечается «это вы» (§11.6). */
  function findById(id: number | null | undefined): AdminUser | null {
    if (!id) return null
    return items.value.find((item) => item.id === id) ?? null
  }

  function describeError(error: unknown, fallback: string): string {
    if (error instanceof ApiError) {
      return error.messages.length > 0 ? error.messages.join('; ') : error.message
    }
    return fallback
  }

  /** Перечитать список; при ошибке показанные строки не затираются. */
  async function load(): Promise<void> {
    isLoading.value = true
    try {
      items.value = await adminsApi.list()
      lastLoadedAt.value = new Date()
      errorMessage.value = ''
    } catch (error) {
      errorMessage.value = describeError(error, 'Не удалось получить список администраторов')
    } finally {
      isLoading.value = false
    }
  }

  /** Создать администратора; `null` — отказ (`409` занятый логин, `422` пароль). */
  async function create(payload: AdminCreatePayload): Promise<AdminUser | null> {
    try {
      const created = await adminsApi.create(payload)
      await load()
      errorMessage.value = ''
      return created
    } catch (error) {
      errorMessage.value = describeError(error, 'Не удалось создать администратора')
      return null
    }
  }

  /** Сменить пароль и/или блокировку; `null` при отказе сервера. */
  async function update(id: number, payload: AdminUpdatePayload): Promise<AdminUser | null> {
    try {
      const updated = await adminsApi.update(id, payload)
      await load()
      errorMessage.value = ''
      return updated
    } catch (error) {
      errorMessage.value = describeError(error, 'Не удалось изменить администратора')
      return null
    }
  }

  /** Блокировка и разблокировка — одна точка `PUT /admins/{id}` (Д-8). */
  async function setActive(id: number, isActive: boolean): Promise<AdminUser | null> {
    return await update(id, { is_active: isActive })
  }

  /** Удалить учётную запись; `false` — отказ (себя/последнего удалить нельзя). */
  async function remove(id: number): Promise<boolean> {
    try {
      await adminsApi.remove(id)
      await load()
      errorMessage.value = ''
      return true
    } catch (error) {
      errorMessage.value = describeError(error, 'Не удалось удалить администратора')
      return false
    }
  }

  return {
    items,
    isLoading,
    errorMessage,
    lastLoadedAt,
    activeCount,
    findById,
    load,
    create,
    update,
    setActive,
    remove,
  }
})
