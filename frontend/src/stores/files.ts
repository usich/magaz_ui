/**
 * Стор матрицы файлов обновления (§11.3, Э4).
 *
 * Матрица — это последний снимок `file_checks` на каждый магазин (§4.5), поэтому
 * стор только читает её и умеет запускать операции: «проверить на выбранных»,
 * «проверить на всех» и «разослать ссылку» (ФН2). Операции идут через
 * `dispatchAction`, то есть одним и тем же путём, что и кнопки магазина.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError } from '@/api/client'
import { dispatchAction, type ActionOutcome, type ActionTargets } from '@/api/actions'
import { filesApi } from '@/api/endpoints'
import type { FileMatrix, FileMatrixRow } from '@/api/types'

export const useFilesStore = defineStore('files', () => {
  const matrix = ref<FileMatrix | null>(null)
  const filename = ref('update')
  const cluster = ref('')
  const selected = ref<number[]>([])
  const isLoading = ref(false)
  const isRunning = ref(false)
  const errorMessage = ref('')
  const lastLoadedAt = ref<Date | null>(null)
  const lastOutcome = ref<ActionOutcome | null>(null)

  const rows = computed<FileMatrixRow[]>(() => matrix.value?.rows ?? [])
  const total = computed(() => matrix.value?.total ?? 0)
  const present = computed(() => matrix.value?.present ?? 0)
  const missing = computed(() => matrix.value?.missing_current ?? 0)
  const clusters = computed(() =>
    Array.from(new Set(rows.value.map((row) => row.cluster_name))).sort(),
  )
  const allSelected = computed(
    () => rows.value.length > 0 && selected.value.length === rows.value.length,
  )

  async function load(): Promise<void> {
    isLoading.value = true
    try {
      matrix.value = await filesApi.matrix({ filename: filename.value, cluster: cluster.value })
      selected.value = selected.value.filter((id) =>
        rows.value.some((row) => row.store_id === id),
      )
      lastLoadedAt.value = new Date()
      errorMessage.value = ''
    } catch (error) {
      errorMessage.value =
        error instanceof ApiError ? error.message : 'Не удалось получить матрицу файлов'
    } finally {
      isLoading.value = false
    }
  }

  function toggle(storeId: number): void {
    selected.value = selected.value.includes(storeId)
      ? selected.value.filter((id) => id !== storeId)
      : [...selected.value, storeId]
  }

  function toggleAll(): void {
    selected.value = allSelected.value ? [] : rows.value.map((row) => row.store_id)
  }

  /** «Проверить на выбранных / на всех» (§11.3). */
  async function checkFiles(stores?: number[] | 'all'): Promise<ActionOutcome | null> {
    return runAction('check_files', { filename: filename.value }, resolveTargets(stores))
  }

  /** «Разослать ссылку» (ФН2): скачать файл, затем сервер сам закажет проверку. */
  async function sendLink(
    url: string,
    stores?: number[] | 'all',
  ): Promise<ActionOutcome | null> {
    return runAction('download_file', { 'url-file': url }, resolveTargets(stores))
  }

  /**
   * Э10: «Удалить на кассе» — действие `delete_file`: убирает `.cf/.cfe/.xml`
   * из рабочей папки агента. Снимок после удаления диспетчер обновит сам, поэтому
   * матрица сразу покажет «файла нет» (§11.3).
   */
  async function deleteOnStore(
    storeId: number,
    suffix?: string | null,
  ): Promise<ActionOutcome | null> {
    return runAction(
      'delete_file',
      { file_name: filename.value, ...(suffix ? { suffix } : {}) },
      { storeIds: [storeId] },
    )
  }

  /** Э10: «Убрать из матрицы» — удалить снимок `file_checks`, файлы на кассе целы. */
  async function dropSnapshot(storeId: number): Promise<boolean> {
    isRunning.value = true
    errorMessage.value = ''
    try {
      await filesApi.dropSnapshot(storeId, filename.value)
      await load()
      return true
    } catch (error) {
      errorMessage.value =
        error instanceof ApiError ? error.message : 'Снимок проверки не удалён'
      return false
    } finally {
      isRunning.value = false
    }
  }

  /**
   * Кому адресована операция: явный кластер, «все» или список магазинов.
   * Пустой список трактуется как «все» — так кнопка «проверить» не даёт `422`.
   */
  function resolveTargets(stores?: number[] | 'all'): ActionTargets {
    if (stores === 'all') return { all: true }
    const ids = stores ?? selected.value
    return ids.length ? { storeIds: ids } : { all: true }
  }

  async function runAction(
    action: 'check_files' | 'download_file' | 'delete_file',
    params: Record<string, unknown>,
    targets: ActionTargets,
  ): Promise<ActionOutcome | null> {
    isRunning.value = true
    errorMessage.value = ''
    try {
      const outcome = await dispatchAction(targets, action, params)
      lastOutcome.value = outcome
      await load()
      return outcome
    } catch (error) {
      errorMessage.value =
        error instanceof ApiError ? error.message : 'Операция не выполнена'
      return null
    } finally {
      isRunning.value = false
    }
  }

  return {
    matrix,
    filename,
    cluster,
    selected,
    isLoading,
    isRunning,
    errorMessage,
    lastLoadedAt,
    lastOutcome,
    rows,
    total,
    present,
    missing,
    clusters,
    allSelected,
    load,
    toggle,
    toggleAll,
    checkFiles,
    sendLink,
    deleteOnStore,
    dropSnapshot,
    runAction,
  }
})