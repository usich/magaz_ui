/**
 * Запуск операций над магазинами (§11.4, Э4) — одна точка для всех форм UI.
 *
 * Контракт различает одиночное действие (`POST /stores/{id}/actions`) и массовое
 * (`POST /bulk/actions`), а интерфейсу нужен один результат: сколько задач
 * поставлено, что пропущено и о чём предупредить. Поэтому здесь выбор
 * получателей и нормализация ответа живут вместе.
 */

import { actionsApi } from './endpoints'
import type { ActionName, ActionPayload, BulkActionResponse } from './types'

export interface ActionTargets {
  /** Магазины из чекбоксов: один или несколько. */
  storeIds?: number[]
  /** Весь кластер (§11.4). */
  cluster?: string | null
  /** Все активные магазины. */
  all?: boolean
}

export interface ActionOutcome {
  /** Что показать в тосте. */
  message: string
  /** Предупреждение Q53 (например, файла на кассе нет) — не блокирует запуск. */
  warning: string | null
  /** Поставленные задачи: id + статус по каждому магазину. */
  jobs: { store_id: number; store_name: string; job_id: number | null; status: string }[]
  /** Сколько магазинов пропущено (отключены). */
  skipped: number
  /** Идентификаторы магазинов, которых коснулась операция. */
  storeIds: number[]
}

function isSingle(targets: ActionTargets): boolean {
  const ids = targets.storeIds ?? []
  return ids.length === 1 && !targets.cluster && !targets.all
}

function outcomeFromBulk(response: BulkActionResponse, fallback: string): ActionOutcome {
  return {
    message: response.message || fallback,
    warning: response.warning,
    jobs: response.jobs.map((job) => ({
      store_id: job.store_id,
      store_name: job.store_name,
      job_id: job.job_id,
      status: job.status,
    })),
    skipped: response.jobs.filter((job) => job.status === 'skipped').length,
    storeIds: response.jobs.map((job) => job.store_id),
  }
}

/** Выполнить операцию: сам магазин → одиночная точка, иначе — массовая. */
export async function dispatchAction(
  targets: ActionTargets,
  action: ActionName,
  params: Record<string, unknown> = {},
): Promise<ActionOutcome> {
  const payload: ActionPayload = { action, params }

  if (isSingle(targets)) {
    const response = await actionsApi.run((targets.storeIds ?? [])[0], payload)
    return {
      message: response.message,
      warning: response.warning,
      jobs: [
        {
          store_id: response.job.store_id,
          store_name: response.job.store_name ?? `#${response.job.store_id}`,
          job_id: response.job.id,
          status: response.job.status,
        },
      ],
      skipped: 0,
      storeIds: [response.job.store_id],
    }
  }

  const response = await actionsApi.runBulk({
    ...payload,
    store_ids: targets.storeIds ?? [],
    cluster: targets.cluster ?? null,
    all: targets.all ?? false,
  })
  return outcomeFromBulk(response, 'Задачи поставлены в очередь')
}