<script setup lang="ts">
/**
 * История операций (§11.6, Э4).
 *
 * Задачи приходят с сервера вместе со счётчиками по статусам: «в работе» — это
 * `queued`/`sent`/`running`, то есть те, за которые сейчас отвечает диспетчер.
 * Список обновляется polling-ом (§11.1), поэтому админ видит переход
 * `202 → 200/400` без перезагрузки страницы.
 */
import { computed, onMounted, onUnmounted } from 'vue'
import { RouterLink } from 'vue-router'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Tag from 'primevue/tag'

import StatusTile from '@/components/StatusTile.vue'
import {
  JOB_KIND_LABELS,
  JOB_STATUS_LABELS,
  jobStatusSeverity,
  useJobsStore,
} from '@/stores/jobs'
import { useStoresStore } from '@/stores/stores'

const jobs = useJobsStore()
const stores = useStoresStore()

const STATUS_OPTIONS = [
  'queued',
  'sent',
  'running',
  'success',
  'failed',
  'timeout',
  'error',
  'canceled',
]

const successful = computed(() => jobs.counts.success ?? 0)
const failed = computed(
  () => (jobs.counts.failed ?? 0) + (jobs.counts.error ?? 0) + (jobs.counts.timeout ?? 0),
)

onMounted(async () => {
  if (!stores.items.length) {
    await stores.load()
  }
  jobs.startPolling()
})

onUnmounted(() => jobs.stopPolling())

function kindLabel(kind: string): string {
  return JOB_KIND_LABELS[kind] ?? kind
}

function statusLabel(status: string): string {
  return JOB_STATUS_LABELS[status] ?? status
}

/** Дата из API приходит в UTC (`Z`); показываем в местном времени (§6). */
function formatDate(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU')
}
</script>

<template>
  <div class="space-y-6">
    <header class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h2 class="text-xl font-semibold">История задач</h2>
        <p class="magaz-muted text-sm">
          Операции магазинов: кто поставил, чем закончилось, полный лог задачи.
        </p>
      </div>
      <div class="flex items-center gap-2">
        <span v-if="jobs.lastLoadedAt" class="magaz-muted text-xs">
          обновлено {{ jobs.lastLoadedAt.toLocaleTimeString('ru-RU') }}
        </span>
        <Button
          label="Обновить"
          icon="pi pi-refresh"
          size="small"
          text
          :loading="jobs.isLoading"
          data-test="jobs-refresh"
          @click="jobs.load()"
        />
      </div>
    </header>

    <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <StatusTile label="Задач в работе" :value="jobs.active" tone="neutral" />
      <StatusTile label="Успешно" :value="successful" tone="positive" />
      <StatusTile label="С ошибкой" :value="failed" tone="negative" />
      <StatusTile label="Всего задач" :value="jobs.total" tone="muted" />
    </div>

    <Message v-if="jobs.errorMessage" severity="error" :closable="false">
      {{ jobs.errorMessage }}
    </Message>

    <section class="rounded-xl border border-slate-200 bg-white p-5">
      <div class="grid gap-3 md:grid-cols-4">
        <label class="space-y-1">
          <span class="magaz-muted text-xs">Магазин</span>
          <select
            v-model="jobs.storeId"
            class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            data-test="jobs-store-filter"
            @change="jobs.load()"
          >
            <option :value="null">Все магазины</option>
            <option v-for="store in stores.items" :key="store.id" :value="store.id">
              {{ store.name }}
            </option>
          </select>
        </label>
        <label class="space-y-1">
          <span class="magaz-muted text-xs">Тип операции</span>
          <select
            v-model="jobs.kind"
            class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            data-test="jobs-kind-filter"
            @change="jobs.load()"
          >
            <option value="">Все операции</option>
            <option v-for="(label, kind) in JOB_KIND_LABELS" :key="kind" :value="kind">
              {{ label }}
            </option>
          </select>
        </label>
        <label class="space-y-1">
          <span class="magaz-muted text-xs">Статус</span>
          <select
            v-model="jobs.status"
            class="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
            data-test="jobs-status-filter"
            @change="jobs.load()"
          >
            <option value="">Любой статус</option>
            <option v-for="status in STATUS_OPTIONS" :key="status" :value="status">
              {{ statusLabel(status) }}
            </option>
          </select>
        </label>
        <label class="magaz-muted flex items-end gap-2 text-sm">
          <input
            v-model="jobs.activeOnly"
            type="checkbox"
            data-test="jobs-active-only"
            @change="jobs.load()"
          />
          только незавершённые
        </label>
      </div>
    </section>

    <section class="overflow-x-auto rounded-xl border border-slate-200 bg-white">
      <table class="magaz-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Магазин</th>
            <th>Операция</th>
            <th>Статус</th>
            <th>Автор</th>
            <th>Попытки</th>
            <th>Создана</th>
            <th>Последнее сообщение</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr v-for="job in jobs.items" :key="job.id" :data-test="`job-row-${job.id}`">
            <td class="magaz-mono">{{ job.id }}</td>
            <td>{{ job.store_name ?? `#${job.store_id}` }}</td>
            <td>{{ kindLabel(job.kind) }}</td>
            <td>
              <Tag
                :value="statusLabel(job.status)"
                :severity="jobStatusSeverity(job.status)"
                data-test="job-status"
              />
            </td>
            <td>{{ job.created_by ?? '—' }}</td>
            <td>{{ job.attempts }}</td>
            <td class="magaz-muted whitespace-nowrap">{{ formatDate(job.created_at) }}</td>
            <td class="max-w-md truncate" :title="job.last_message ?? job.last_error ?? ''">
              {{ job.last_message ?? job.last_error ?? '—' }}
            </td>
            <td>
              <RouterLink
                :to="{ name: 'job-detail', params: { id: job.id } }"
                data-test="job-open"
              >
                <Button label="Лог" icon="pi pi-list" size="small" text />
              </RouterLink>
            </td>
          </tr>
          <tr v-if="!jobs.items.length && !jobs.isLoading">
            <td colspan="9" class="magaz-muted py-6 text-center" data-test="jobs-empty">
              Задач нет: запустите операцию в карточке магазина или на странице файлов.
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

