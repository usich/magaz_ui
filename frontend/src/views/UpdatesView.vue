<script setup lang="ts">
/** Обновлятор (§Д-19): пульт обновления 1С — статусы магазинов, массовые операции, журнал. */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'

import { ApiError } from '@/api/client'
import { jobsApi, updateCyclesApi } from '@/api/endpoints'
import type { CurrentCycle, JobItem, LocalBaseItem } from '@/api/types'
import StoreOperationStrip from '@/components/StoreOperationStrip.vue'
import StatusTile from '@/components/StatusTile.vue'
import LocalBaseFormDialog from '@/components/LocalBaseFormDialog.vue'
import LocalBaseUpdateDialog from '@/components/LocalBaseUpdateDialog.vue'
import LocalOperationPanel from '@/components/LocalOperationPanel.vue'
import { useFilesStore } from '@/stores/files'
import { useLocalBasesStore } from '@/stores/local_bases'
import { JOB_KIND_LABELS, JOB_STATUS_LABELS, jobStatusSeverity } from '@/stores/jobs'
import { useStoresStore } from '@/stores/stores'

const stores = useStoresStore()
const files = useFilesStore()
const localBases = useLocalBasesStore()
const toast = useToast()

// Вкладки «Обновлятора»
const activeTab = ref<'stores' | 'local'>('stores')

// Локальные базы 1С (Д-20): форма «добавить/изменить»
const baseFormVisible = ref(false)
const editingBase = ref<InstanceType<typeof LocalBaseFormDialog>['$props']['base']>(null)
function openBaseCreate(): void {
  editingBase.value = null
  baseFormVisible.value = true
}
function openBaseEdit(base: NonNullable<typeof editingBase.value>): void {
  editingBase.value = base
  baseFormVisible.value = true
}
function onBaseSaved(): void {
  baseFormVisible.value = false
  void localBases.load()
}
async function removeLocalBase(baseId: number): Promise<void> {
  await localBases.remove(baseId)
  toast.add({ severity: 'success', summary: 'База отключена', life: 4000 })
}

// Выбор магазинов
const selected = ref<number[]>([])
const allSelected = computed(() => stores.items.length > 0 && selected.value.length === stores.items.length)
function toggle(storeId: number): void {
  selected.value = selected.value.includes(storeId)
    ? selected.value.filter((id) => id !== storeId)
    : [...selected.value, storeId]
}
function toggleAll(): void {
  selected.value = allSelected.value ? [] : stores.items.map((item) => item.id)
}

// Задачи этапов (Д-29): статусы считаются только по задачам **текущего цикла**,
// поэтому успех прошлой недели не светится после старта нового прохода, а старая
// ошибка не висит красным после успешного повтора.
const cycleInfo = ref<CurrentCycle | null>(null)
const localCycleInfo = ref<CurrentCycle | null>(null)
//: Задачи текущего цикла магазинов (все виды: проверка/скачивание/слияние/обновление)
const cycleJobs = ref<JobItem[]>([])
//: Задачи текущего цикла локальных баз
const localCycleJobs = ref<JobItem[]>([])
//: История `run_update_db` за всё время — только для колонки «Последнее обновление»
const updateHistory = ref<JobItem[]>([])
const localUpdateHistory = ref<JobItem[]>([])
const recentJobs = ref<JobItem[]>([])
const jobsLoading = ref(false)
//: Журнал операций: по умолчанию свернут, разворачивается кнопкой.
const logExpanded = ref(false)
//: Д-29: показывать в журнале только задачи текущего цикла
const journalCycleOnly = ref(false)
const isCycleStarting = ref(false)

async function loadJobs(): Promise<void> {
  jobsLoading.value = true
  try {
    const [storesCycle, localCycle] = await Promise.all([
      updateCyclesApi.current('stores'),
      updateCyclesApi.current('local'),
    ])
    cycleInfo.value = storesCycle
    localCycleInfo.value = localCycle
    const storesCycleId = storesCycle.cycle?.id ?? null
    const localCycleId = localCycle.cycle?.id ?? null

    const [storesJobs, localJobsPage, updatePage, localUpdatePage, recentPage] =
      await Promise.all([
        storesCycleId ? jobsApi.list({ cycleId: storesCycleId, limit: 500 }) : null,
        localCycleId ? jobsApi.list({ cycleId: localCycleId, limit: 500 }) : null,
        jobsApi.list({ kind: 'run_update_db', limit: 200 }),
        jobsApi.list({ kind: 'run_update_db_local', limit: 200 }),
        jobsApi.list({
          kind: '',
          limit: 40,
          cycleId: journalCycleOnly.value ? storesCycleId : null,
        }),
      ])
    cycleJobs.value = storesJobs?.items ?? []
    localCycleJobs.value = localJobsPage?.items ?? []
    updateHistory.value = updatePage.items
    localUpdateHistory.value = localUpdatePage.items
    recentJobs.value = recentPage.items
  } finally {
    jobsLoading.value = false
  }
}

/** Начать новый цикл обновления (Д-29): статусы прошлого прохода очищаются. */
async function startNewCycle(scope: 'stores' | 'local'): Promise<void> {
  isCycleStarting.value = true
  try {
    const result = await updateCyclesApi.openNew({ scope, transfer_active: true })
    toast.add({
      severity: 'success',
      summary: 'Начат новый цикл',
      detail: result.message,
      life: 5000,
    })
    await loadJobs()
  } catch (error) {
    const message =
      error instanceof ApiError ? error.message : 'Не удалось начать новый цикл'
    toast.add({ severity: 'error', summary: 'Новый цикл не начат', detail: message, life: 6000 })
  } finally {
    isCycleStarting.value = false
  }
}

// Д-25/Д-28: диалог операции локальной базы — только массовый запуск.
// Построчные кнопки убраны: одну базу обновляют так же, как пачку (чекбокс +
// кнопка в шапке таблицы), иначе UI провоцировал «двойной» запуск одной ИБ.
const bulkBaseIds = ref<number[]>([])
const localDialogMode = ref<'check' | 'merge' | 'update'>('update')
const localUpdateVisible = computed({
  get: () => bulkBaseIds.value.length > 0,
  set: (value: boolean) => {
    if (!value) {
      bulkBaseIds.value = []
    }
  },
})
function openLocalBulkUpdate(): void {
  if (!localSelected.value.length) return
  bulkBaseIds.value = [...localSelected.value]
  localDialogMode.value = 'update'
  localUpdateVisible.value = true
}
function openLocalBulkMerge(): void {
  if (!localSelected.value.length) return
  bulkBaseIds.value = [...localSelected.value]
  localDialogMode.value = 'merge'
  localUpdateVisible.value = true
}
function openLocalBulkCheck(): void {
  if (!localSelected.value.length) return
  bulkBaseIds.value = [...localSelected.value]
  localDialogMode.value = 'check'
  localUpdateVisible.value = true
}
function onLocalUpdateSubmitted(): void {
  void loadJobs()
}

// Д-25: выбор нескольких баз для массового обновления (аналог магазинов)
const localSelected = ref<number[]>([])
const localAllSelected = computed(
  () => localBases.items.length > 0 && localSelected.value.length === localBases.items.length,
)
function toggleLocalSelect(baseId: number): void {
  localSelected.value = localSelected.value.includes(baseId)
    ? localSelected.value.filter((id) => id !== baseId)
    : [...localSelected.value, baseId]
}
function toggleLocalAll(): void {
  localSelected.value = localAllSelected.value ? [] : localBases.items.map((item) => item.id)
}

// Д-группы: выбор базы/группы. Чекбокс группы выбирает/снимает все базы этой группы.
function localBasesOfGroup(group: string): LocalBaseItem[] {
  return localBases.items.filter((item) => item.group === group)
}
function localGroupAllSelected(group: string): boolean {
  const ids = localBasesOfGroup(group).map((item) => item.id)
  return ids.length > 0 && ids.every((id) => localSelected.value.includes(id))
}
function toggleLocalGroup(group: string): void {
  const ids = localBasesOfGroup(group).map((item) => item.id)
  if (localGroupAllSelected(group)) {
    localSelected.value = localSelected.value.filter((id) => !ids.includes(id))
  } else {
    localSelected.value = Array.from(new Set([...localSelected.value, ...ids]))
  }
}


/** Статусы «в работе» (§4.3): задача ещё не завершена. */
const ACTIVE_JOB_STATUSES = ['queued', 'sent', 'running']
/** Статусы провала: именно они дают красный текст в колонке «Статус задачи». */
const FAILED_JOB_STATUSES = ['failed', 'timeout', 'error']

/** Самая свежая задача из списка (по `created_at`, при равенстве — по `id`). */
function latestJob(jobs: JobItem[]): JobItem | null {
  let best: JobItem | null = null
  for (const job of jobs) {
    if (best === null) {
      best = job
      continue
    }
    const newer =
      (job.created_at ?? '') > (best.created_at ?? '') ||
      ((job.created_at ?? '') === (best.created_at ?? '') && job.id > best.id)
    if (newer) best = job
  }
  return best
}

/**
 * Задача этапа магазина **в текущем цикле** (Д-29).
 *
 * Раньше здесь бралась последняя задача данного вида за всю историю — из-за
 * этого успешное слияние прошлого прохода светилось зелёным и после старта
 * нового, а старая ошибка висела красным после успешного повтора.
 */
function stageOf(storeId: number, kind: string): JobItem | null {
  return latestJob(
    cycleJobs.value.filter((job) => job.store_id === storeId && job.kind === kind),
  )
}

/** Задача этапа локальной базы в текущем цикле (Д-29). */
function localStageOf(baseId: number, kind: string): JobItem | null {
  return latestJob(
    localCycleJobs.value.filter((job) => job.local_base_id === baseId && job.kind === kind),
  )
}

function stageStatus(job: JobItem | null): { label: string; severity: string } {
  if (!job) return { label: '—', severity: 'secondary' }
  return { label: JOB_STATUS_LABELS[job.status] ?? job.status, severity: jobStatusSeverity(job.status) }
}

/** Последнее успешное обновление БД магазина за всё время (факт, а не статус этапа). */
function lastUpdateOkAt(storeId: number): string {
  const job = latestJob(
    updateHistory.value.filter((item) => item.store_id === storeId && item.status === 'success'),
  )
  return job ? formatTime(job.finished_at) : '—'
}

/** Последнее успешное обновление локальной базы за всё время (Д-24). */
function lastLocalUpdateOkAt(baseId: number): string {
  const job = latestJob(
    localUpdateHistory.value.filter(
      (item) => item.local_base_id === baseId && item.status === 'success',
    ),
  )
  return job ? formatTime(job.finished_at) : '—'
}

const counts = computed(() => {
  const active = stores.items.filter((item) => item.is_active)
  const online = active.filter((item) => item.is_online)
  const ready = files.rows.filter((row) => row.cf_exists || row.cfe_exists).length
  const activeJobs = [...cycleJobs.value, ...localCycleJobs.value].filter((job) =>
    ACTIVE_JOB_STATUSES.includes(job.status),
  )
  return { active: active.length, online: online.length, offline: active.length - online.length, ready, activeJobs: activeJobs.length }
})

function formatTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU')
}

/**
 * Текст ошибки в колонке «Статус задачи» (Д-29).
 *
 * Показываем ошибку только если **последняя** задача магазина в текущем цикле
 * провалилась: после успешного повтора (пусть и новой задачей) красный текст
 * гаснет. Раньше здесь искалась любая `failed`-задача из 200 последних.
 */
function storeError(storeId: number): string {
  const last = latestJob(cycleJobs.value.filter((job) => job.store_id === storeId))
  if (!last || !FAILED_JOB_STATUSES.includes(last.status)) return ''
  return last.last_error ?? 'Задача не выполнена'
}

/** Что сейчас выполняется по магазину в текущем цикле. */
function storeActivity(storeId: number): string {
  const parts: string[] = []
  for (const job of cycleJobs.value) {
    if (job.store_id !== storeId) continue
    if (!ACTIVE_JOB_STATUSES.includes(job.status)) continue
    const kind = JOB_KIND_LABELS[job.kind] ?? job.kind
    const status = JOB_STATUS_LABELS[job.status] ?? job.status
    parts.push(`${kind}: ${status}`)
  }
  return Array.from(new Set(parts)).join(' · ') || '—'
}

/** Подпись бейджа текущего цикла магазинов (Д-29). */
const cycleLabel = computed(() => {
  const cycle = cycleInfo.value?.cycle
  if (!cycle) return 'Цикл ещё не начат'
  const parts = [`Цикл №${cycle.id}`, `открыт ${formatTime(cycle.started_at)}`]
  parts.push(`задач: ${cycle.stats.total}`)
  if (cycle.stats.active) parts.push(`в работе: ${cycle.stats.active}`)
  return parts.join(' · ')
})

/** Тон бейджа: жёлтый — в цикле уже есть завершённые этапы (пора начинать новый). */
const cycleTone = computed(() => {
  if (!cycleInfo.value?.cycle) return 'secondary'
  return cycleInfo.value.suggest_new ? 'warn' : 'info'
})

/** То же для вкладки локальных баз. */
const localCycleLabel = computed(() => {
  const cycle = localCycleInfo.value?.cycle
  if (!cycle) return 'Цикл ещё не начат'
  const parts = [`Цикл №${cycle.id}`, `открыт ${formatTime(cycle.started_at)}`]
  parts.push(`задач: ${cycle.stats.total}`)
  if (cycle.stats.active) parts.push(`в работе: ${cycle.stats.active}`)
  return parts.join(' · ')
})
const localCycleTone = computed(() => {
  if (!localCycleInfo.value?.cycle) return 'secondary'
  return localCycleInfo.value.suggest_new ? 'warn' : 'info'
})

/** Переключить режим журнала «только текущий цикл» и перечитать его. */
function toggleJournalCycle(): void {
  journalCycleOnly.value = !journalCycleOnly.value
  void loadJobs()
}

let timer: ReturnType<typeof setInterval> | null = null
onMounted(() => {
  void Promise.all([stores.load(), files.load(), localBases.load(), loadJobs()])
  timer = setInterval(() => {
    void Promise.all([stores.load(), files.load(), localBases.load(), loadJobs()])
  }, 10_000)
})
onUnmounted(() => {
  if (timer !== null) clearInterval(timer)
})
</script>

<template>
  <div class="space-y-6">
    <div class="flex gap-2 border-b border-slate-200 pb-2" data-test="updates-tabs">
      <button
        class="rounded-lg px-4 py-2 text-sm font-medium"
        :class="activeTab === 'stores' ? 'bg-blue-50 text-blue-700' : 'magaz-muted'"
        data-test="updates-tab-stores"
        @click="activeTab = 'stores'"
      >
        Магазины
      </button>
      <button
        class="rounded-lg px-4 py-2 text-sm font-medium"
        :class="activeTab === 'local' ? 'bg-blue-50 text-blue-700' : 'magaz-muted'"
        data-test="updates-tab-local"
        @click="activeTab = 'local'"
      >
        Локальные базы
      </button>
    </div>

    <div v-if="activeTab === 'stores'" class="space-y-6">
      <section class="grid grid-cols-2 gap-4 lg:grid-cols-3 xl:grid-cols-5">
        <StatusTile label="Магазинов в работе" :value="counts.active" icon="pi pi-building" />
        <StatusTile label="Онлайн" :value="counts.online" tone="positive" icon="pi pi-check-circle" />
        <StatusTile label="Офлайн" :value="counts.offline" tone="negative" icon="pi pi-times-circle" />
        <StatusTile label="Файлы готовы" :value="counts.ready" icon="pi pi-file" />
        <StatusTile label="Операций в работе" :value="counts.activeJobs" icon="pi pi-refresh" />
      </section>

      <StoreOperationStrip
        :store-ids="selected"
        :all="!selected.length"
        :all-stores="stores.items"
        title="Массовые операции по магазинам"
        @submitted="loadJobs"
      />

      <section class="rounded-xl border border-slate-200 bg-white p-5">
        <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 class="font-semibold">Магазины и статусы обновления</h2>
          <div class="flex flex-wrap items-center gap-2">
            <!-- Д-29: статусы этапов считаются только по текущему циклу -->
            <Tag
              :value="cycleLabel"
              :severity="cycleTone"
              :title="
                cycleInfo?.suggest_new
                  ? 'В цикле уже есть завершённые этапы — вероятно, пора начать новый'
                  : 'Статусы этапов относятся к этому циклу'
              "
              data-test="updates-cycle-badge"
            />
            <Button
              label="Начать новый цикл"
              size="small"
              severity="secondary"
              outlined
              :loading="isCycleStarting"
              data-test="updates-cycle-new"
              @click="startNewCycle('stores')"
            />
            <Button
              label="Выбрать все"
              severity="secondary"
              size="small"
              outlined
              :data-test="`updates-select-all-${allSelected ? 'on' : 'off'}`"
              @click="toggleAll"
            />
          </div>
        </div>

        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="magaz-muted border-b border-slate-200 text-left text-xs">
                <th class="py-2 pr-3 font-medium"><span class="sr-only">выбор</span></th>
                <th class="py-2 pr-3 font-medium">Магазин</th>
                <th class="py-2 pr-3 font-medium">Онлайн</th>
                <th class="py-2 pr-3 font-medium">Проверка</th>
                <th class="py-2 pr-3 font-medium">Merge</th>
                <th class="py-2 pr-3 font-medium">Update</th>
                <th class="py-2 pr-3 font-medium">Последнее обновление</th>
                <th class="py-2 pr-3 font-medium">Статус задачи</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="store in stores.items"
                :key="store.id"
                class="border-b border-slate-100"
                :data-test="`updates-store-${store.id}`"
              >
                <td class="py-2 pr-3">
                  <Checkbox
                    :model-value="selected.includes(store.id)"
                    binary
                    :disabled="!store.is_active"
                    :data-test="`updates-select-${store.id}`"
                    @update:model-value="toggle(store.id)"
                  />
                </td>
                <td class="py-2 pr-3">
                  <RouterLink
                    :to="{ name: 'store-detail', params: { id: store.id } }"
                    class="font-medium text-blue-700"
                  >
                    {{ store.name }}
                  </RouterLink>
                  <div class="magaz-muted text-xs">{{ store.ip_address }} · {{ store.cluster_name }}</div>
                </td>
                <td class="py-2 pr-3 whitespace-nowrap">
                  <Tag
                    :value="store.is_online ? 'онлайн' : 'офлайн'"
                    :severity="store.is_online ? 'success' : 'danger'"
                    :data-test="`updates-online-${store.id}`"
                  />
                  <div v-if="store.agent_version" class="magaz-muted text-xs">
                    агент {{ store.agent_version }}
                  </div>
                </td>
                <td class="py-2 pr-3 whitespace-nowrap">
                  <Tag
                    :value="stageStatus(stageOf(store.id, 'check_files')).label"
                    :severity="stageStatus(stageOf(store.id, 'check_files')).severity"
                    :data-test="`updates-check-${store.id}`"
                  />
                </td>
                <td class="py-2 pr-3 whitespace-nowrap">
                  <Tag
                    :value="stageStatus(stageOf(store.id, 'run_merge')).label"
                    :severity="stageStatus(stageOf(store.id, 'run_merge')).severity"
                    :data-test="`updates-merge-${store.id}`"
                  />
                </td>
                <td class="py-2 pr-3 whitespace-nowrap">
                  <Tag
                    :value="stageStatus(stageOf(store.id, 'run_update_db')).label"
                    :severity="stageStatus(stageOf(store.id, 'run_update_db')).severity"
                    :data-test="`updates-update-${store.id}`"
                  />
                </td>
                <td class="py-2 pr-3 whitespace-nowrap magaz-muted">
                  {{ lastUpdateOkAt(store.id) }}
                </td>
                <td class="py-2 pr-3">
                  <div v-if="storeError(store.id)" class="text-rose-600 text-xs">{{ storeError(store.id) }}</div>
                  <div v-else class="magaz-muted text-xs">{{ storeActivity(store.id) }}</div>
                </td>
              </tr>
              <tr v-if="!stores.items.length && !stores.isLoading">
                <td colspan="8" class="magaz-muted py-6 text-center">Магазинов нет.</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="rounded-xl border border-slate-200 bg-white p-5">
        <button
          class="flex w-full items-center justify-between gap-2 text-left"
          data-test="updates-log-toggle"
          @click="logExpanded = !logExpanded"
        >
          <h2 class="font-semibold">Журнал операций</h2>
          <span class="magaz-muted text-sm">
            <i class="pi" :class="logExpanded ? 'pi-chevron-up' : 'pi-chevron-down'"></i>
          </span>
        </button>
        <label v-if="logExpanded" class="mt-3 flex items-center gap-2 text-sm">
          <Checkbox
            :model-value="journalCycleOnly"
            binary
            data-test="updates-log-cycle-only"
            @update:model-value="toggleJournalCycle"
          />
          <span>Только текущий цикл</span>
        </label>
        <div v-if="logExpanded" class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="magaz-muted border-b border-slate-200 text-left text-xs">
                <th class="py-2 pr-3 font-medium">Магазин</th>
                <th class="py-2 pr-3 font-medium">Операция</th>
                <th class="py-2 pr-3 font-medium">Цикл</th>
                <th class="py-2 pr-3 font-medium">Статус</th>
                <th class="py-2 pr-3 font-medium">Начато</th>
                <th class="py-2 pr-3 font-medium">Завершено</th>
                <th class="py-2 pr-3 font-medium">Лог</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="job in recentJobs" :key="job.id" class="border-b border-slate-100">
                <td class="py-2 pr-3">{{ job.store_name }}</td>
                <td class="py-2 pr-3">{{ JOB_KIND_LABELS[job.kind] ?? job.kind }}</td>
                <td class="py-2 pr-3 magaz-muted whitespace-nowrap">
                  {{ job.cycle_id ? `№${job.cycle_id}` : '—' }}
                </td>
                <td class="py-2 pr-3">
                  <Tag :value="JOB_STATUS_LABELS[job.status] ?? job.status" :severity="jobStatusSeverity(job.status)" />
                </td>
                <td class="py-2 pr-3 magaz-muted whitespace-nowrap">{{ formatTime(job.started_at) }}</td>
                <td class="py-2 pr-3 magaz-muted whitespace-nowrap">{{ formatTime(job.finished_at) }}</td>
                <td class="py-2 pr-3">
                  <span v-if="job.log_files.length" class="magaz-mono text-xs">{{ job.log_files.join(', ') }}</span>
                  <span v-else-if="job.last_error" class="text-rose-600 text-xs">{{ job.last_error }}</span>
                  <span v-else class="magaz-muted text-xs">—</span>
                </td>
              </tr>
              <tr v-if="!recentJobs.length && !jobsLoading">
                <td colspan="7" class="magaz-muted py-6 text-center">Операций ещё не было.</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>

    <div v-else class="space-y-6">
      <LocalOperationPanel
        :local-base-ids="localSelected"
        :all="!localSelected.length"
        :all-bases="localBases.items"
        title="Массовые операции по локальным базам"
        @submitted="loadJobs"
      />

      <section class="rounded-xl border border-slate-200 bg-white p-5">
        <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 class="font-semibold">Локальные базы и статусы обновления</h2>
          <div class="flex flex-wrap gap-2">
            <!-- Д-29: свой цикл для локальных баз — «новый цикл» магазинов его не трогает -->
            <Tag
              :value="localCycleLabel"
              :severity="localCycleTone"
              :title="
                localCycleInfo?.suggest_new
                  ? 'В цикле уже есть завершённые этапы — вероятно, пора начать новый'
                  : 'Статусы этапов относятся к этому циклу'
              "
              data-test="updates-local-cycle-badge"
            />
            <Button
              label="Начать новый цикл"
              size="small"
              severity="secondary"
              outlined
              :loading="isCycleStarting"
              data-test="updates-local-cycle-new"
              @click="startNewCycle('local')"
            />
            <Button
              label="Выбрать все"
              severity="secondary"
              size="small"
              outlined
              :data-test="`updates-local-select-all-${localAllSelected ? 'on' : 'off'}`"
              @click="toggleLocalAll"
            />
            <Button
              label="Проверить файлы"
              size="small"
              :disabled="!localSelected.length"
              data-test="updates-local-check-selected"
              @click="openLocalBulkCheck"
            />
            <Button
              label="Слияние…"
              size="small"
              :disabled="!localSelected.length"
              data-test="updates-local-merge-selected"
              @click="openLocalBulkMerge"
            />
            <Button
              label="Обновить базу…"
              size="small"
              :disabled="!localSelected.length"
              data-test="updates-local-update-selected"
              @click="openLocalBulkUpdate"
            />
            <Button label="Добавить базу" icon="pi pi-plus" size="small" data-test="updates-local-add" @click="openBaseCreate" />
          </div>
        </div>


        <div v-if="!localBases.items.length && !localBases.isLoading" class="magaz-muted py-6 text-center">
          Локальные базы ещё не добавлены — нажмите «Добавить базу».
        </div>

        <div v-else class="space-y-4">
          <section
            v-for="group in localBases.groups"
            :key="group"
            class="rounded-xl border border-slate-200 bg-white"
            :data-test="`lb-group-${group || 'без-группы'}`"
          >
            <header class="flex items-center gap-3 border-b border-slate-200 px-4 py-2">
              <Checkbox
                :model-value="localGroupAllSelected(group)"
                binary
                :data-test="`lb-group-select-${group || 'без-группы'}`"
                @update:model-value="toggleLocalGroup(group)"
              />
              <h3 class="font-semibold text-blue-700">{{ group || 'Без группы' }}</h3>
              <span class="magaz-muted text-xs">баз: {{ localBasesOfGroup(group).length }}</span>
            </header>
            <div class="overflow-x-auto">
              <table class="w-full text-sm">
                <thead>
                  <tr class="magaz-muted border-b border-slate-100 text-left text-xs">
                    <th class="px-4 py-2 font-medium"><span class="sr-only">выбор</span></th>
                    <th class="px-4 py-2 font-medium">База</th>
                    <th class="py-2 pr-3 font-medium">Версия 1С</th>
                    <th class="py-2 pr-3 font-medium">Кластер / БД</th>
                    <th class="py-2 pr-3 font-medium">Путь к базе</th>
                    <th class="py-2 pr-3 font-medium">Расширение</th>
                    <th class="py-2 pr-3 font-medium">Слияние</th>
                    <th class="py-2 pr-3 font-medium">Обновление</th>
                    <th class="py-2 pr-3 font-medium">Последнее обновление</th>
                    <th class="py-2 pr-3 text-right font-medium">Действия</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="base in localBasesOfGroup(group)"
                    :key="base.id"
                    class="border-b border-slate-100 last:border-0"
                    :data-test="`lb-row-${base.id}`"
                  >
                    <td class="px-4 py-2">
                      <Checkbox
                        :model-value="localSelected.includes(base.id)"
                        binary
                        :data-test="`lb-select-${base.id}`"
                        @update:model-value="toggleLocalSelect(base.id)"
                      />
                    </td>
                    <td class="px-4 py-2 font-medium">
                      {{ base.name }}
                      <div v-if="base.comment" class="magaz-muted text-xs">{{ base.comment }}</div>
                    </td>
                    <td class="py-2 pr-3 whitespace-nowrap">{{ base.version_1c }}</td>
                    <td class="py-2 pr-3">
                      <div>{{ base.cluster_name }}</div>
                      <div class="magaz-muted text-xs">{{ base.database_name }}</div>
                    </td>
                    <td class="py-2 pr-3">
                      <span v-if="base.base_path" class="magaz-mono text-xs">{{ base.base_path }}</span>
                      <span v-else class="magaz-muted text-xs">—</span>
                    </td>
                    <td class="py-2 pr-3">
                      <span v-if="base.extension_name" class="magaz-mono text-xs">{{ base.extension_name }}</span>
                      <span v-else class="magaz-muted text-xs">—</span>
                    </td>
                    <td class="py-2 pr-3 whitespace-nowrap">
                      <Tag
                        :value="stageStatus(localStageOf(base.id, 'run_merge_local')).label"
                        :severity="stageStatus(localStageOf(base.id, 'run_merge_local')).severity"
                        :data-test="`lb-merge-status-${base.id}`"
                      />
                    </td>
                    <td class="py-2 pr-3 whitespace-nowrap">
                      <Tag
                        :value="stageStatus(localStageOf(base.id, 'run_update_db_local')).label"
                        :severity="stageStatus(localStageOf(base.id, 'run_update_db_local')).severity"
                        :data-test="`lb-status-${base.id}`"
                      />
                    </td>
                    <td class="py-2 pr-3 whitespace-nowrap magaz-muted">
                      {{ lastLocalUpdateOkAt(base.id) }}
                    </td>
                    <td class="py-2 pr-3 text-right whitespace-nowrap">
                      <Button label="Изменить" size="small" text :data-test="`lb-edit-${base.id}`" @click="openBaseEdit(base)" />
                      <Button label="Отключить" size="small" severity="danger" text :data-test="`lb-remove-${base.id}`" @click="removeLocalBase(base.id)" />
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>
        </div>
      </section>

      <section class="rounded-xl border border-slate-200 bg-white p-5">
        <h2 class="mb-3 font-semibold">Журнал операций по локальным базам</h2>
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="magaz-muted border-b border-slate-200 text-left text-xs">
                <th class="py-2 pr-3 font-medium">База</th>
                <th class="py-2 pr-3 font-medium">Цикл</th>
                <th class="py-2 pr-3 font-medium">Статус</th>
                <th class="py-2 pr-3 font-medium">Начато</th>
                <th class="py-2 pr-3 font-medium">Завершено</th>
                <th class="py-2 pr-3 font-medium">Лог</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="job in localCycleJobs" :key="job.id" class="border-b border-slate-100">
                <td class="py-2 pr-3">{{ job.local_base_name }}</td>
                <td class="py-2 pr-3 magaz-muted whitespace-nowrap">
                  {{ job.cycle_id ? `№${job.cycle_id}` : '—' }}
                </td>
                <td class="py-2 pr-3">
                  <Tag :value="JOB_STATUS_LABELS[job.status] ?? job.status" :severity="jobStatusSeverity(job.status)" />
                </td>
                <td class="py-2 pr-3 magaz-muted whitespace-nowrap">{{ formatTime(job.started_at) }}</td>
                <td class="py-2 pr-3 magaz-muted whitespace-nowrap">{{ formatTime(job.finished_at) }}</td>
                <td class="py-2 pr-3">
                  <span v-if="job.log_files.length" class="magaz-mono text-xs">{{ job.log_files.join(', ') }}</span>
                  <span v-else-if="job.last_error" class="text-rose-600 text-xs">{{ job.last_error }}</span>
                  <span v-else class="magaz-muted text-xs">—</span>
                </td>
              </tr>
              <tr v-if="!localCycleJobs.length && !jobsLoading">
                <td colspan="6" class="magaz-muted py-6 text-center">Обновлений локальных баз ещё не было.</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>
  </div>

  <LocalBaseFormDialog v-model:visible="baseFormVisible" :base="editingBase" @saved="onBaseSaved" />
  <LocalBaseUpdateDialog
    v-model:visible="localUpdateVisible"
    :bulk-base-ids="bulkBaseIds"
    :mode="localDialogMode"
    @submitted="onLocalUpdateSubmitted"
  />
</template>

