<script setup lang="ts">
/**
 * Локальные базы 1С (Д-20): отдельная страница `/local-bases` для управления.
 *
 * Таблица с группировкой по полю `group` (одинаковые значения — одна секция),
 * CRUD через `LocalBaseFormDialog` и мягкое удаление (отключение). Статус проверки
 * бэкапа (Д-21) показывается в строке, чтобы было видно, есть ли бэкап.
 */
import { computed, onMounted, ref } from 'vue'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'

import { ApiError } from '@/api/client'
import { jobsApi, localBasesApi } from '@/api/endpoints'
import type { JobItem, LocalBaseItem } from '@/api/types'
import LocalBaseFormDialog from '@/components/LocalBaseFormDialog.vue'
import LocalBaseUpdateDialog from '@/components/LocalBaseUpdateDialog.vue'
import { useLocalBasesStore } from '@/stores/local_bases'
import { JOB_STATUS_LABELS, jobStatusSeverity } from '@/stores/jobs'

const toast = useToast()
const store = useLocalBasesStore()

const formVisible = ref(false)
const editingBase = ref<LocalBaseItem | null>(null)
const search = ref('')

const visibleItems = computed(() => {
  const query = search.value.trim().toLowerCase()
  if (!query) return store.items
  return store.items.filter((item) =>
    [item.name, item.database_name, item.group, item.cluster_name]
      .join(' ')
      .toLowerCase()
      .includes(query),
  )
})

// Д-24/Д-26: операции локальных баз по видам (обновление / слияние / проверка файлов)
const localJobs = ref<JobItem[]>([])
const localMergeJobs = ref<JobItem[]>([])
const localCheckJobs = ref<JobItem[]>([])
async function loadJobs(): Promise<void> {
  const [update, merge, check] = await Promise.all([
    jobsApi.list({ kind: 'run_update_db_local', limit: 200 }),
    jobsApi.list({ kind: 'run_merge_local', limit: 200 }),
    jobsApi.list({ kind: 'check_files_local', limit: 200 }),
  ])
  localJobs.value = update.items
  localMergeJobs.value = merge.items
  localCheckJobs.value = check.items
}

/** Последняя задача по локальной базе в заданном списке (Д-24). */
function lastLocalFor(jobs: JobItem[], localBaseId: number): JobItem | null {
  let best: JobItem | null = null
  for (const job of jobs) {
    if (job.local_base_id !== localBaseId) continue
    if (best === null || (job.created_at ?? '') > (best.created_at ?? '')) best = job
  }
  return best
}

// Диалог операции локальной базы (Д-24/Д-26); Д-25: массовое обновление
const updateBase = ref<LocalBaseItem | null>(null)
const bulkBaseIds = ref<number[]>([])
const localDialogMode = ref<'merge' | 'update'>('update')
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
async function runLocalBulkCheck(): Promise<void> {
  if (!localSelected.value.length) return
  try {
    const response = await localBasesApi.bulkUpdate({
      local_base_ids: [...localSelected.value],
      kind: 'check_files_local',
      file_name: 'update',
    })
    toast.add({
      severity: 'success',
      summary: 'Проверка файлов поставлена в очередь',
      detail: response.message,
      life: 5000,
    })
    void loadJobs()
  } catch (error) {
    const message =
      error instanceof ApiError ? error.message : 'Не удалось поставить проверку файлов'
    toast.add({ severity: 'error', summary: 'Операция не выполнена', detail: message, life: 6000 })
  }
}
async function runLocalCheck(base: LocalBaseItem): Promise<void> {
  try {
    const response = await localBasesApi.checkFiles(base.id, { file_name: 'update' })
    toast.add({
      severity: 'success',
      summary: 'Проверка файлов поставлена в очередь',
      detail: response.message,
      life: 5000,
    })
    void loadJobs()
  } catch (error) {
    const message =
      error instanceof ApiError ? error.message : 'Не удалось поставить проверку файлов'
    toast.add({ severity: 'error', summary: 'Операция не выполнена', detail: message, life: 6000 })
  }
}
function onLocalUpdateSubmitted(): void {
  void loadJobs()
}

// Д-25: выбор нескольких баз для массового обновления
const localSelected = ref<number[]>([])
const localAllSelected = computed(
  () => store.items.length > 0 && localSelected.value.length === store.items.length,
)
function toggleLocalSelect(baseId: number): void {
  localSelected.value = localSelected.value.includes(baseId)
    ? localSelected.value.filter((id) => id !== baseId)
    : [...localSelected.value, baseId]
}
function toggleLocalAll(): void {
  localSelected.value = localAllSelected.value ? [] : store.items.map((item) => item.id)
}

function stageStatus(job: JobItem | null): { label: string; severity: string } {
  if (!job) return { label: '—', severity: 'secondary' }
  return { label: JOB_STATUS_LABELS[job.status] ?? job.status, severity: jobStatusSeverity(job.status) }
}

onMounted(() => {
  void store.load()
  void loadJobs()
})

function openCreate(): void {
  editingBase.value = null
  copyFrom.value = null
  formVisible.value = true
}

function openEdit(base: LocalBaseItem): void {
  editingBase.value = base
  copyFrom.value = null
  formVisible.value = true
}

/** «Скопировать» (Д-23): создать базу по образцу — все поля, кроме имени. */
const copyFrom = ref<LocalBaseItem | null>(null)
function openCopy(base: LocalBaseItem): void {
  copyFrom.value = base
  editingBase.value = null
  formVisible.value = true
}
function onSaved(): void {
  formVisible.value = false
  void store.load()
}

function backupBadge(status: string): string {
  if (status === 'ok') return 'есть'
  if (status === 'missing') return 'нет'
  return 'не проверено'
}

async function removeBase(base: LocalBaseItem): Promise<void> {
  try {
    await store.remove(base.id)
    toast.add({ severity: 'success', summary: 'База отключена', detail: base.name, life: 4000 })
  } catch (error) {
    const message = error instanceof ApiError ? error.message : 'Не удалось отключить базу'
    toast.add({ severity: 'error', summary: 'Операция не выполнена', detail: message, life: 6000 })
  }
}
</script>

<template>
  <div class="space-y-4">
    <Message v-if="store.errorMessage" severity="error" :closable="false">
      {{ store.errorMessage }}
    </Message>

    <div class="flex flex-wrap items-center justify-between gap-2">
      <div class="flex items-center gap-2">
        <InputText
          v-model="search"
          placeholder="Поиск: имя, БД, группа"
          class="w-72"
          data-test="lb-search"
        />
        <span class="magaz-muted text-sm">всего {{ store.items.length }}</span>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <Button
          label="Выбрать все"
          severity="secondary"
          size="small"
          outlined
          :data-test="`lb-select-all-${localAllSelected ? 'on' : 'off'}`"
          @click="toggleLocalAll"
        />
        <Button
          label="Проверить файлы"
          size="small"
          :disabled="!localSelected.length"
          data-test="lb-check-selected"
          @click="runLocalBulkCheck"
        />
        <Button
          label="Слияние…"
          size="small"
          :disabled="!localSelected.length"
          data-test="lb-merge-selected"
          @click="openLocalBulkMerge"
        />
        <Button
          label="Обновить базу…"
          size="small"
          :disabled="!localSelected.length"
          data-test="lb-update-selected"
          @click="openLocalBulkUpdate"
        />
        <Button
          label="Добавить базу"
          icon="pi pi-plus"
          size="small"
          data-test="lb-add"
          @click="openCreate"
        />
      </div>
    </div>

    <div v-if="store.groups.length" class="space-y-4">
      <section
        v-for="group in store.groups"
        :key="group"
        class="rounded-xl border border-slate-200 bg-white"
        :data-test="`lb-group-${group || 'без-группы'}`"
      >
        <header class="border-b border-slate-200 px-4 py-2">
          <h2 class="font-semibold text-blue-700">{{ group || 'Без группы' }}</h2>
        </header>
        <table class="w-full text-sm">
          <thead>
            <tr class="magaz-muted border-b border-slate-100 text-left text-xs">
              <th class="px-4 py-2 font-medium"><span class="sr-only">выбор</span></th>
              <th class="px-4 py-2 font-medium">Название</th>
              <th class="py-2 pr-3 font-medium">Версия 1С</th>
              <th class="py-2 pr-3 font-medium">Кластер / БД</th>
              <th class="py-2 pr-3 font-medium">Пользователь</th>
              <th class="py-2 pr-3 font-medium">Путь к базе</th>
              <th class="py-2 pr-3 font-medium">Бэкап</th>
              <th class="py-2 pr-3 font-medium">Проверка</th>
              <th class="py-2 pr-3 font-medium">Слияние</th>
              <th class="py-2 pr-3 font-medium">Обновление</th>
              <th class="py-2 pr-3 text-right font-medium">Действия</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="base in visibleItems.filter((item) => item.group === group)"
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
              <td class="py-2 pr-3 whitespace-nowrap">{{ base.user_name_1c }}</td>
              <td class="py-2 pr-3">
                <span v-if="base.base_path" class="magaz-mono text-xs">{{ base.base_path }}</span>
                <span v-else class="magaz-muted text-xs">—</span>
              </td>
              <td class="py-2 pr-3">
                <Tag
                  :value="backupBadge(base.backup_status)"
                  :severity="base.backup_status === 'ok' ? 'success' : base.backup_status === 'missing' ? 'danger' : 'secondary'"
                />
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
              <td class="py-2 pr-3 text-right whitespace-nowrap">
                <Button
                  label="Проверить файлы"
                  size="small"
                  text
                  :data-test="`lb-check-${base.id}`"
                  @click="runLocalCheck(base)"
                />
                <Button
                  label="Слияние…"
                  size="small"
                  text
                  :data-test="`lb-merge-${base.id}`"
                  @click="openLocalMerge(base)"
                />
                <Button
                  label="Обновить базу"
                  size="small"
                  text
                  :data-test="`lb-update-${base.id}`"
                  @click="openLocalUpdate(base)"
                />
                <Button label="Скопировать" size="small" text :data-test="`lb-copy-${base.id}`" @click="openCopy(base)" />
                <Button
                  label="Изменить"
                  size="small"
                  text
                  :data-test="`lb-edit-${base.id}`"
                  @click="openEdit(base)"
                />
                <Button
                  label="Отключить"
                  size="small"
                  severity="danger"
                  text
                  :data-test="`lb-remove-${base.id}`"
                  @click="removeBase(base)"
                />
              </td>
            </tr>
            <tr v-if="!visibleItems.filter((item) => item.group === group).length">
              <td colspan="11" class="magaz-muted py-4 text-center">Нет баз в этой группе</td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>

    <div v-else class="magaz-muted rounded-xl border border-dashed border-slate-300 p-6 text-sm">
      Локальные базы ещё не добавлены — нажмите «Добавить базу».
    </div>

    <LocalBaseFormDialog v-model:visible="formVisible" :base="editingBase" :copy-from="copyFrom" @saved="onSaved" />
    <LocalBaseUpdateDialog
      v-model:visible="localUpdateVisible"
      :base="updateBase"
      :bulk-base-ids="bulkBaseIds"
      :mode="localDialogMode"
      @submitted="onLocalUpdateSubmitted"
    />
  </div>
</template>

