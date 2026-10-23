<script setup lang="ts">
/** Обновлятор (§Д-19): пульт обновления 1С — статусы магазинов, массовые операции, журнал. */
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import RadioButton from 'primevue/radiobutton'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'

import { dispatchAction, type ActionOutcome } from '@/api/actions'
import { jobsApi } from '@/api/endpoints'
import type { JobItem, LocalBaseItem } from '@/api/types'
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

// Задачи этапов
const mergeJobs = ref<JobItem[]>([])
const updateJobs = ref<JobItem[]>([])
const recentJobs = ref<JobItem[]>([])
// Д-24/Д-26: операции локальных баз по видам (обновление / слияние / проверка файлов)
const localJobs = ref<JobItem[]>([])
const localMergeJobs = ref<JobItem[]>([])
const localCheckJobs = ref<JobItem[]>([])
const jobsLoading = ref(false)

async function loadJobs(): Promise<void> {
  jobsLoading.value = true
  try {
    const [merge, update, recent, local, localMerge, localCheck] = await Promise.all([
      jobsApi.list({ kind: 'run_merge', limit: 200 }),
      jobsApi.list({ kind: 'run_update_db', limit: 200 }),
      jobsApi.list({ kind: '', limit: 40 }),
      jobsApi.list({ kind: 'run_update_db_local', limit: 200 }),
      jobsApi.list({ kind: 'run_merge_local', limit: 200 }),
      jobsApi.list({ kind: 'check_files_local', limit: 200 }),
    ])
    mergeJobs.value = merge.items
    updateJobs.value = update.items
    recentJobs.value = recent.items
    localJobs.value = local.items
    localMergeJobs.value = localMerge.items
    localCheckJobs.value = localCheck.items
  } finally {
    jobsLoading.value = false
  }
}

// Д-24/Д-26: диалог операции локальной базы (merge/update), Д-25: массовое обновление.
const updateBase = ref<LocalBaseItem | null>(null)
const bulkBaseIds = ref<number[]>([])
const localDialogMode = ref<'check' | 'merge' | 'update'>('update')
const localUpdateVisible = computed({
  get: () => updateBase.value !== null || bulkBaseIds.value.length > 0,
  set: (value: boolean) => {
    if (!value) {
      updateBase.value = null
      bulkBaseIds.value = []
    }
  },
})
function openLocalUpdate(base: LocalBaseItem): void {
  bulkBaseIds.value = []
  localDialogMode.value = 'update'
  updateBase.value = base
  localUpdateVisible.value = true
}
function openLocalMerge(base: LocalBaseItem): void {
  bulkBaseIds.value = []
  localDialogMode.value = 'merge'
  updateBase.value = base
  localUpdateVisible.value = true
}
function openLocalBulkUpdate(): void {
  if (!localSelected.value.length) return
  updateBase.value = null
  bulkBaseIds.value = [...localSelected.value]
  localDialogMode.value = 'update'
  localUpdateVisible.value = true
}
function openLocalBulkMerge(): void {
  if (!localSelected.value.length) return
  updateBase.value = null
  bulkBaseIds.value = [...localSelected.value]
  localDialogMode.value = 'merge'
  localUpdateVisible.value = true
}
function openLocalBulkCheck(): void {
  if (!localSelected.value.length) return
  updateBase.value = null
  bulkBaseIds.value = [...localSelected.value]
  localDialogMode.value = 'check'
  localUpdateVisible.value = true
}
function openLocalCheck(base: LocalBaseItem): void {
  bulkBaseIds.value = []
  localDialogMode.value = 'check'
  updateBase.value = base
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

function lastFor(jobs: JobItem[], storeId: number): JobItem | null {
  let best: JobItem | null = null
  for (const job of jobs) {
    if (job.store_id !== storeId) continue
    if (best === null || (job.created_at ?? '') > (best.created_at ?? '')) best = job
  }
  return best
}

/** Последняя задача обновления по локальной базе (Д-24). */
function lastLocalFor(jobs: JobItem[], localBaseId: number): JobItem | null {
  let best: JobItem | null = null
  for (const job of jobs) {
    if (job.local_base_id !== localBaseId) continue
    if (best === null || (job.created_at ?? '') > (best.created_at ?? '')) best = job
  }
  return best
}

function stageStatus(job: JobItem | null): { label: string; severity: string } {
  if (!job) return { label: '—', severity: 'secondary' }
  return { label: JOB_STATUS_LABELS[job.status] ?? job.status, severity: jobStatusSeverity(job.status) }
}

function lastOkAt(job: JobItem | null): string {
  if (!job || job.status !== 'success') return '—'
  return formatTime(job.finished_at)
}

function fileRows(storeId: number) {
  return files.rows.filter((row) => row.store_id === storeId)
}

const counts = computed(() => {
  const active = stores.items.filter((item) => item.is_active)
  const online = active.filter((item) => item.is_online)
  const ready = files.rows.filter((row) => row.cf_exists || row.cfe_exists).length
  const activeJobs = [...mergeJobs.value, ...updateJobs.value].filter((job) =>
    ['queued', 'sent', 'running'].includes(job.status),
  )
  return { active: active.length, online: online.length, offline: active.length - online.length, ready, activeJobs: activeJobs.length }
})

// Компактные операции по выбору
const opDialog = ref<null | 'merge' | 'update'>(null)
const mergeForm = reactive({ file_name: 'update', is_extension: false, extension_name: '' })
const updateForm = reactive({ is_fast_update: false, is_extension: false, extension_name: '' })
function openDialog(kind: 'merge' | 'update'): void {
  opDialog.value = kind
}
const mergeDialogVisible = computed({
  get: () => opDialog.value === 'merge',
  set: (value: boolean) => {
    if (!value) opDialog.value = null
  },
})
const updateDialogVisible = computed({
  get: () => opDialog.value === 'update',
  set: (value: boolean) => {
    if (!value) opDialog.value = null
  },
})
function runSelectedOp(kind: 'merge' | 'update'): void {
  if (!selected.value.length) return
  void (async () => {
    const targets = { storeIds: [...selected.value] }
    const outcome =
      kind === 'merge'
        ? await dispatchAction(targets, 'run_merge', {
            file_name: mergeForm.file_name.trim(),
            is_extension: mergeForm.is_extension,
            ...(mergeForm.is_extension && mergeForm.extension_name.trim()
              ? { extension_name: mergeForm.extension_name.trim() }
              : {}),
          })
        : await dispatchAction(targets, 'run_update_db', {
            is_fast_update: updateForm.is_fast_update,
            is_extension: updateForm.is_extension,
            ...(updateForm.is_extension && updateForm.extension_name.trim()
              ? { extension_name: updateForm.extension_name.trim() }
              : {}),
          })
    opDialog.value = null
    notify(outcome, kind === 'merge' ? 'Слияние поставлено в очередь' : 'Обновление БД поставлено в очередь')
  })()
}

async function runSelectedCheck(): Promise<void> {
  if (!selected.value.length) return
  const outcome = await dispatchAction({ storeIds: [...selected.value] }, 'check_files', { filename: 'update' })
  notify(outcome, 'Проверка файлов поставлена в очередь')
}

function notify(outcome: ActionOutcome | null, summary: string): void {
  toast.add({
    severity: outcome?.warning ? 'warn' : 'success',
    summary,
    detail: outcome?.warning ?? outcome?.message ?? 'Задачи поставлены в очередь',
    life: 6000,
  })
  void loadJobs()
}

function formatTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU')
}
function fileMark(exists: boolean): string {
  return exists ? '✓' : '✗'
}
function fileCell(exists: boolean): string {
  return exists ? 'text-emerald-600' : 'text-rose-600'
}
function storeError(storeId: number): string {
  const failed = [...mergeJobs.value, ...updateJobs.value].find(
    (j) => j.store_id === storeId && j.status === 'failed',
  )
  return failed?.last_error ?? ''
}
function storeActivity(storeId: number): string {
  const parts: string[] = []
  const merge = lastFor(mergeJobs.value, storeId)
  const update = lastFor(updateJobs.value, storeId)
  if (merge && merge.status === 'running') parts.push('merge выполняется')
  if (update && update.status === 'running') parts.push('update выполняется')
  return parts.join(' · ') || '—'
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
          <div class="flex flex-wrap gap-2">
            <Button
              label="Выбрать все"
              severity="secondary"
              size="small"
              outlined
              :data-test="`updates-select-all-${allSelected ? 'on' : 'off'}`"
              @click="toggleAll"
            />
            <Button
              label="Проверить файлы"
              size="small"
              :disabled="!selected.length"
              data-test="updates-check-selected"
              @click="runSelectedCheck"
            />
            <Button
              label="Слияние…"
              size="small"
              :disabled="!selected.length"
              data-test="updates-merge-selected"
              @click="openDialog('merge')"
            />
            <Button
              label="Обновить базу…"
              size="small"
              :disabled="!selected.length"
              data-test="updates-update-selected"
              @click="openDialog('update')"
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
                <th class="py-2 pr-3 font-medium">Файлы</th>
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
                  <template v-for="(row, i) in fileRows(store.id)" :key="i">
                    <span :class="fileCell(row.cf_exists)" class="magaz-mono mr-2">{{ fileMark(row.cf_exists) }}</span>
                    <span :class="fileCell(row.cfe_exists)" class="magaz-mono mr-2">{{ fileMark(row.cfe_exists) }}</span>
                    <span :class="fileCell(row.xml_exists)" class="magaz-mono">{{ fileMark(row.xml_exists) }}</span>
                  </template>
                  <div v-if="!fileRows(store.id).length" class="magaz-muted text-xs">нет снимка</div>
                </td>
                <td class="py-2 pr-3 whitespace-nowrap">
                  <Tag
                    :value="stageStatus(lastFor(mergeJobs, store.id)).label"
                    :severity="stageStatus(lastFor(mergeJobs, store.id)).severity"
                  />
                </td>
                <td class="py-2 pr-3 whitespace-nowrap">
                  <Tag
                    :value="stageStatus(lastFor(updateJobs, store.id)).label"
                    :severity="stageStatus(lastFor(updateJobs, store.id)).severity"
                  />
                </td>
                <td class="py-2 pr-3 whitespace-nowrap magaz-muted">
                  {{ lastOkAt(lastFor(updateJobs, store.id)) }}
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
        <h2 class="mb-3 font-semibold">Журнал операций</h2>
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="magaz-muted border-b border-slate-200 text-left text-xs">
                <th class="py-2 pr-3 font-medium">Магазин</th>
                <th class="py-2 pr-3 font-medium">Операция</th>
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
                <td colspan="6" class="magaz-muted py-6 text-center">Операций ещё не было.</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <Dialog v-model:visible="mergeDialogVisible" header="Слияние конфигурации" modal :style="{ width: '480px' }">
        <div class="space-y-3">
          <label class="block space-y-1">
            <span class="magaz-muted text-xs">Имя файла (без расширения)</span>
            <InputText v-model="mergeForm.file_name" class="w-full" data-test="updates-merge-file" />
          </label>
          <div class="flex items-center gap-4 text-sm">
            <span class="magaz-muted text-xs">Сливаем:</span>
            <label class="flex items-center gap-1"><RadioButton v-model="mergeForm.is_extension" :value="false" data-test="updates-merge-cf" /><span>Основная (.cf)</span></label>
            <label class="flex items-center gap-1"><RadioButton v-model="mergeForm.is_extension" :value="true" data-test="updates-merge-cfe" /><span>Расширение (.cfe)</span></label>
          </div>
          <label v-if="mergeForm.is_extension" class="block space-y-1">
            <span class="magaz-muted text-xs">Имя расширения</span>
            <InputText v-model="mergeForm.extension_name" class="w-full" placeholder="имя расширения" />
          </label>
        </div>
        <template #footer>
          <Button label="Отмена" text size="small" @click="opDialog = null" />
          <Button label="Запустить" size="small" :disabled="!mergeForm.file_name.trim()" data-test="updates-merge-run" @click="runSelectedOp('merge')" />
        </template>
      </Dialog>

      <Dialog v-model:visible="updateDialogVisible" header="Обновление конфигурации БД" modal :style="{ width: '480px' }">
        <div class="space-y-3">
          <div class="flex items-center gap-4 text-sm">
            <span class="magaz-muted text-xs">Обновляем:</span>
            <label class="flex items-center gap-1"><RadioButton v-model="updateForm.is_extension" :value="false" data-test="updates-update-cf" /><span>Основная</span></label>
            <label class="flex items-center gap-1"><RadioButton v-model="updateForm.is_extension" :value="true" data-test="updates-update-cfe" /><span>Расширение (.cfe)</span></label>
          </div>
          <label v-if="updateForm.is_extension" class="block space-y-1">
            <span class="magaz-muted text-xs">Имя расширения</span>
            <InputText v-model="updateForm.extension_name" class="w-full" placeholder="имя расширения" />
          </label>
          <label class="flex items-center gap-2 text-sm">
            <Checkbox v-model="updateForm.is_fast_update" binary />
            <span>Быстрое обновление (без завершения сеансов)</span>
          </label>
        </div>
        <template #footer>
          <Button label="Отмена" text size="small" @click="opDialog = null" />
          <Button label="Запустить" size="small" data-test="updates-update-run" @click="runSelectedOp('update')" />
        </template>
      </Dialog>
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


        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="magaz-muted border-b border-slate-200 text-left text-xs">
                <th class="py-2 pr-3 font-medium"><span class="sr-only">выбор</span></th>
                <th class="py-2 pr-3 font-medium">База</th>
                <th class="py-2 pr-3 font-medium">Версия 1С</th>
                <th class="py-2 pr-3 font-medium">Кластер / БД</th>
                <th class="py-2 pr-3 font-medium">Путь к базе</th>
                <th class="py-2 pr-3 font-medium">Расширение</th>
                <th class="py-2 pr-3 font-medium">Проверка</th>
                <th class="py-2 pr-3 font-medium">Слияние</th>
                <th class="py-2 pr-3 font-medium">Обновление</th>
                <th class="py-2 pr-3 font-medium">Последнее обновление</th>
                <th class="py-2 pr-3 text-right font-medium">Действия</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="base in localBases.items"
                :key="base.id"
                class="border-b border-slate-100"
                :data-test="`lb-row-${base.id}`"
              >
                <td class="py-2 pr-3">
                  <Checkbox
                    :model-value="localSelected.includes(base.id)"
                    binary
                    :data-test="`lb-select-${base.id}`"
                    @update:model-value="toggleLocalSelect(base.id)"
                  />
                </td>
                <td class="py-2 pr-3 font-medium">
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
                    :value="stageStatus(lastLocalFor(localCheckJobs, base.id)).label"
                    :severity="stageStatus(lastLocalFor(localCheckJobs, base.id)).severity"
                    :data-test="`lb-check-status-${base.id}`"
                  />
                </td>
                <td class="py-2 pr-3 whitespace-nowrap">
                  <Tag
                    :value="stageStatus(lastLocalFor(localMergeJobs, base.id)).label"
                    :severity="stageStatus(lastLocalFor(localMergeJobs, base.id)).severity"
                    :data-test="`lb-merge-status-${base.id}`"
                  />
                </td>
                <td class="py-2 pr-3 whitespace-nowrap">
                  <Tag
                    :value="stageStatus(lastLocalFor(localJobs, base.id)).label"
                    :severity="stageStatus(lastLocalFor(localJobs, base.id)).severity"
                    :data-test="`lb-status-${base.id}`"
                  />
                </td>
                <td class="py-2 pr-3 whitespace-nowrap magaz-muted">
                  {{ lastOkAt(lastLocalFor(localJobs, base.id)) }}
                </td>
                <td class="py-2 pr-3 text-right whitespace-nowrap">
                  <Button label="Проверить файлы" size="small" text :data-test="`lb-check-${base.id}`" @click="openLocalCheck(base)" />
                  <Button label="Слияние…" size="small" text :data-test="`lb-merge-${base.id}`" @click="openLocalMerge(base)" />
                  <Button label="Обновить базу" size="small" text :data-test="`lb-update-${base.id}`" @click="openLocalUpdate(base)" />
                  <Button label="Изменить" size="small" text :data-test="`lb-edit-${base.id}`" @click="openBaseEdit(base)" />
                  <Button label="Отключить" size="small" severity="danger" text :data-test="`lb-remove-${base.id}`" @click="removeLocalBase(base.id)" />
                </td>
              </tr>
              <tr v-if="!localBases.items.length && !localBases.isLoading">
                <td colspan="11" class="magaz-muted py-6 text-center">Локальные базы ещё не добавлены — нажмите «Добавить базу».</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="rounded-xl border border-slate-200 bg-white p-5">
        <h2 class="mb-3 font-semibold">Журнал операций по локальным базам</h2>
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="magaz-muted border-b border-slate-200 text-left text-xs">
                <th class="py-2 pr-3 font-medium">База</th>
                <th class="py-2 pr-3 font-medium">Статус</th>
                <th class="py-2 pr-3 font-medium">Начато</th>
                <th class="py-2 pr-3 font-medium">Завершено</th>
                <th class="py-2 pr-3 font-medium">Лог</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="job in localJobs" :key="job.id" class="border-b border-slate-100">
                <td class="py-2 pr-3">{{ job.local_base_name }}</td>
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
              <tr v-if="!localJobs.length && !jobsLoading">
                <td colspan="5" class="magaz-muted py-6 text-center">Обновлений локальных баз ещё не было.</td>
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
    :base="updateBase"
    :bulk-base-ids="bulkBaseIds"
    :mode="localDialogMode"
    @submitted="onLocalUpdateSubmitted"
  />
</template>

