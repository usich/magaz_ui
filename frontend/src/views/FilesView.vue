<script setup lang="ts">
/**
 * Матрица файлов обновления (§11.3, Э4).
 *
 * Показывает последний снимок проверки на каждый магазин: есть ли `.cf`, `.cfe`,
 * `.xml`, их размер и дату, и когда файлы проверялись. Отсюда же запускаются
 * массовые операции: «проверить» и «разослать ссылку» (ФН2) — сервер ставит
 * задачи в очередь и сам добирает повторную проверку после скачивания.
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import { useToast } from 'primevue/usetoast'

import type { ActionOutcome } from '@/api/actions'
import type { FileMatrixRow } from '@/api/types'
import ConfirmDeleteDialog from '@/components/ConfirmDeleteDialog.vue'
import { useFilesStore } from '@/stores/files'

const files = useFilesStore()
const toast = useToast()

const isLinkOpen = ref(false)
const link = reactive({ url: '' })

const selectedCount = computed(() => files.selected.length)
const allSelected = computed(() => files.allSelected)

onMounted(() => files.load())

async function runCheck(stores?: number[] | 'all'): Promise<void> {
  const outcome = await files.checkFiles(stores)
  notifyOutcome(outcome, 'Проверка поставлена в очередь')
}

async function runSendLink(): Promise<void> {
  const outcome = await files.sendLink(link.url.trim(), 'all')
  if (outcome) {
    isLinkOpen.value = false
    link.url = ''
  }
  notifyOutcome(outcome, 'Скачивание поставлено в очередь')
}

async function runSendLinkSelected(): Promise<void> {
  const outcome = await files.sendLink(link.url.trim(), files.selected)
  notifyOutcome(outcome, 'Скачивание поставлено в очередь')
}

function notifyOutcome(outcome: ActionOutcome | null, summary: string): void {
  if (!outcome) {
    if (files.errorMessage) {
      toast.add({
        severity: 'error',
        summary: 'Операция не выполнена',
        detail: files.errorMessage,
        life: 6000,
      })
    }
    return
  }
  toast.add({
    severity: outcome.warning ? 'warn' : 'success',
    summary,
    detail: outcome.warning ?? outcome.message,
    life: 6000,
  })
}

function formatBytes(size: number | null): string {
  if (size === null || size === undefined) return '—'
  if (size < 1024) return `${size} Б`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} КБ`
  return `${(size / (1024 * 1024)).toFixed(1)} МБ`
}

function formatDate(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU')
}

function mark(exists: boolean): string {
  return exists ? '✓' : '✗'
}

function cellClass(exists: boolean): string {
  return exists ? 'text-emerald-600' : 'text-rose-600'
}

/** Э10: «Удалить на кассе» — подтверждение, потому что файл уходит с диска кассы. */
const pendingDelete = ref<FileMatrixRow | null>(null)
const isDeleting = ref(false)

function askDeleteOnStore(row: FileMatrixRow): void {
  pendingDelete.value = row
}

async function confirmDeleteOnStore(): Promise<void> {
  const row = pendingDelete.value
  if (!row) return
  isDeleting.value = true
  try {
    const outcome = await files.deleteOnStore(row.store_id)
    if (outcome) {
      pendingDelete.value = null
    }
    notifyOutcome(outcome, 'Удаление файлов на кассе поставлено в очередь')
  } finally {
    isDeleting.value = false
  }
}

/** Э10: «Убрать из матрицы» — удаляется снимок проверки, файлы на кассе целы. */
async function dropSnapshot(row: FileMatrixRow): Promise<void> {
  if (await files.dropSnapshot(row.store_id)) {
    toast.add({
      severity: 'success',
      summary: 'Снимок убран из матрицы',
      detail: `${row.store_name}: строка снова «не проверялся», пока касса не пришлёт новый снимок`,
      life: 5000,
    })
    return
  }
  toast.add({
    severity: 'error',
    summary: 'Снимок не удалён',
    detail: files.errorMessage,
    life: 6000,
  })
}

/** Магазин без файла — красная строка (§11.3). */
function rowClass(hasFile: boolean): string {
  return hasFile ? '' : 'bg-rose-50'
}
</script>

<template>
  <div class="space-y-6">
    <header class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h2 class="text-xl font-semibold">Файлы обновления</h2>
        <p class="magaz-muted text-sm">
          Матрица «магазин × (cf/cfe/xml)» с размерами и датами последней проверки.
        </p>
      </div>
      <div class="flex items-center gap-2">
        <span v-if="files.lastLoadedAt" class="magaz-muted text-xs">
          обновлено {{ files.lastLoadedAt.toLocaleTimeString('ru-RU') }}
        </span>
        <Button
          label="Обновить"
          icon="pi pi-refresh"
          size="small"
          text
          :loading="files.isLoading"
          data-test="files-refresh"
          @click="files.load()"
        />
      </div>
    </header>

    <div class="rounded-xl border border-slate-200 bg-white p-5">
      <div class="flex flex-wrap items-end gap-3">
        <label class="space-y-1">
          <span class="magaz-muted text-xs">Имя файла (без расширения)</span>
          <InputText
            v-model="files.filename"
            class="w-48"
            data-test="files-filename"
            @keyup.enter="files.load()"
          />
        </label>
        <label class="space-y-1">
          <span class="magaz-muted text-xs">Кластер</span>
          <select
            v-model="files.cluster"
            class="rounded-lg border border-slate-300 px-3 py-2 text-sm"
            data-test="files-cluster"
            @change="files.load()"
          >
            <option value="">Все кластеры</option>
            <option v-for="item in files.clusters" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <Button
          label="Проверить на всех"
          icon="pi pi-search"
          size="small"
          :loading="files.isRunning"
          data-test="files-check-all"
          @click="runCheck('all')"
        />
        <Button
          label="Проверить выбранные"
          icon="pi pi-check-square"
          size="small"
          severity="secondary"
          :disabled="selectedCount === 0"
          data-test="files-check-selected"
          @click="runCheck(files.selected)"
        />
        <Button
          label="Разослать ссылку"
          icon="pi pi-download"
          size="small"
          severity="warn"
          data-test="files-open-link"
          @click="isLinkOpen = true"
        />
      </div>

      <p class="magaz-muted mt-3 text-sm" data-test="files-summary">
        Файл «{{ files.matrix?.filename ?? files.filename }}» есть у
        <strong>{{ files.present }}</strong> из <strong>{{ files.total }}</strong> магазинов ·
        выбран магазинов: {{ selectedCount }}
      </p>
    </div>

    <Message v-if="files.errorMessage" severity="error" :closable="false">
      {{ files.errorMessage }}
    </Message>

    <section class="overflow-x-auto rounded-xl border border-slate-200 bg-white">
      <table class="magaz-table">
        <thead>
          <tr>
            <th>
              <input
                type="checkbox"
                :checked="allSelected"
                data-test="files-select-all"
                @change="files.toggleAll()"
              />
            </th>
            <th>Магазин</th>
            <th>IP</th>
            <th>Кластер</th>
            <th>cf</th>
            <th>cfe</th>
            <th>xml</th>
            <th>Проверено</th>
            <th class="w-56">Действия</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in files.rows"
            :key="row.store_id"
            :class="rowClass(row.has_file)"
            :data-test="`file-row-${row.store_id}`"
          >
            <td>
              <input
                type="checkbox"
                :checked="files.selected.includes(row.store_id)"
                data-test="files-select-row"
                @change="files.toggle(row.store_id)"
              />
            </td>
            <td>
              <RouterLink
                :to="{ name: 'store-detail', params: { id: row.store_id } }"
                class="text-sky-700 hover:underline"
              >
                {{ row.store_name }}
              </RouterLink>
              <span v-if="!row.is_active" class="magaz-muted text-xs"> · отключён</span>
              <span v-else-if="!row.is_online" class="magaz-muted text-xs"> · офлайн</span>
            </td>
            <td class="magaz-mono">{{ row.ip_address }}</td>
            <td>{{ row.cluster_name }}</td>
            <td :class="cellClass(row.cf_exists)">
              <span class="magaz-mono" data-test="cf-cell">{{ mark(row.cf_exists) }}</span>
              <span class="magaz-muted text-xs">
                {{ formatBytes(row.cf_size) }} · {{ formatDate(row.cf_mtime) }}
              </span>
            </td>
            <td :class="cellClass(row.cfe_exists)">
              <span class="magaz-mono">{{ mark(row.cfe_exists) }}</span>
              <span class="magaz-muted text-xs">
                {{ formatBytes(row.cfe_size) }} · {{ formatDate(row.cfe_mtime) }}
              </span>
            </td>
            <td :class="cellClass(row.xml_exists)">
              <span class="magaz-mono">{{ mark(row.xml_exists) }}</span>
              <span class="magaz-muted text-xs">
                {{ formatBytes(row.xml_size) }} · {{ formatDate(row.xml_mtime) }}
              </span>
            </td>
            <td class="magaz-muted whitespace-nowrap" data-test="file-checked-at">
              {{ row.checked ? formatDate(row.checked_at) : 'не проверялся' }}
            </td>
            <td>
              <div class="flex flex-wrap gap-1">
                <Button
                  label="Удалить на кассе"
                  size="small"
                  text
                  severity="danger"
                  :disabled="!row.is_active"
                  :data-test="`file-delete-store-${row.store_id}`"
                  @click="askDeleteOnStore(row)"
                />
                <Button
                  label="Убрать из матрицы"
                  size="small"
                  text
                  :disabled="!row.checked"
                  :data-test="`file-drop-snapshot-${row.store_id}`"
                  @click="dropSnapshot(row)"
                />
              </div>
            </td>
          </tr>
          <tr v-if="!files.rows.length && !files.isLoading">
            <td colspan="9" class="magaz-muted py-6 text-center" data-test="files-empty">
              Магазинов нет: добавьте их на странице «Магазины».
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <Dialog
      v-model:visible="isLinkOpen"
      header="Разослать ссылку на пакет обновления"
      modal
      :style="{ width: '620px' }"
    >
      <p class="magaz-muted text-sm">
        Имя файла берётся из ссылки как basename (FR-1), после скачивания сервер сам закажет
        повторную проверку файлов (§11.3).
      </p>
      <InputText
        v-model="link.url"
        class="mt-3 w-full"
        placeholder="https://disk.yandex.ru/d/XXXX"
        data-test="link-dialog-url"
      />
      <template #footer>
        <Button label="Отмена" text size="small" @click="isLinkOpen = false" />
        <Button
          label="Только выбранным"
          size="small"
          severity="secondary"
          :disabled="!link.url.trim() || selectedCount === 0"
          data-test="link-dialog-selected"
          @click="runSendLinkSelected"
        />
        <Button
          label="Всем магазинам"
          size="small"
          :disabled="!link.url.trim()"
          data-test="link-dialog-all"
          @click="runSendLink"
        />
      </template>
    </Dialog>

    <ConfirmDeleteDialog
      :visible="pendingDelete !== null"
      header="Удалить файлы обновления на кассе?"
      :message="`С кассы магазина «${pendingDelete?.store_name ?? ''}» будут удалены файлы «${files.filename}» (.cf/.cfe/.xml).`"
      :details="[
        'Удаляются только файлы обновления в рабочей папке агента: логи 1С и файлы базы не трогаются',
        'После удаления снимок проверки обновляется сам: матрица сразу покажет «файла нет»',
        'Файл придётся скачать заново («Разослать ссылку»), если он ещё нужен',
      ]"
      :loading="isDeleting"
      @update:visible="(value: boolean) => { if (!value) pendingDelete = null }"
      @confirm="confirmDeleteOnStore"
    />
  </div>
</template>

