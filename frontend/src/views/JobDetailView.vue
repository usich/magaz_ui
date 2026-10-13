<script setup lang="ts">
/**
 * Карточка задачи `/jobs/{id}` (§11.6, Э4).
 *
 * Показывает то, что сервер сохранил о задаче: параметры запроса к агенту (без
 * паролей — П-1), статус, попытки, имена лог-файлов на кассе и полный журнал
 * `job_logs`. Хвост лога 1С лежит в `excerpt_preview` — его удобно читать прямо
 * здесь, не открывая `/jobs`.
 */
import { computed, onMounted } from 'vue'
import { RouterLink } from 'vue-router'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Tag from 'primevue/tag'

import {
  JOB_KIND_LABELS,
  JOB_STATUS_LABELS,
  jobStatusSeverity,
  useJobsStore,
} from '@/stores/jobs'

const props = defineProps<{ id: string }>()

const jobs = useJobsStore()
const jobId = computed(() => Number(props.id))
const job = computed(() => jobs.detail)

onMounted(() => jobs.loadDetail(jobId.value))

function kindLabel(kind: string): string {
  return JOB_KIND_LABELS[kind] ?? kind
}

function statusLabel(status: string): string {
  return JOB_STATUS_LABELS[status] ?? status
}

/** Severity строки журнала: ошибки красные, предупреждения жёлтые (§4.4). */
function levelSeverity(level: string): 'danger' | 'warn' | 'info' {
  if (level === 'error') return 'danger'
  if (level === 'warning') return 'warn'
  return 'info'
}

function levelLabel(level: string): string {
  return { error: 'ошибка', warning: 'внимание', info: 'инфо' }[level] ?? level
}

function formatDate(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU')
}

const paramsJson = computed(() =>
  job.value?.params ? JSON.stringify(job.value.params, null, 2) : '—',
)
</script>

<template>
  <div class="space-y-6">
    <header class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <RouterLink to="/jobs" class="magaz-muted text-xs" data-test="job-back">
          ← все задачи
        </RouterLink>
        <h2 class="mt-1 text-xl font-semibold">
          Задача #{{ jobId }}
          <span v-if="job" class="magaz-muted text-base">· {{ kindLabel(job.kind) }}</span>
        </h2>
      </div>
      <div class="flex items-center gap-2">
        <Tag
          v-if="job"
          :value="statusLabel(job.status)"
          :severity="jobStatusSeverity(job.status)"
        />
        <Button
          label="Обновить"
          icon="pi pi-refresh"
          size="small"
          text
          :loading="jobs.isDetailLoading"
          data-test="job-refresh"
          @click="jobs.loadDetail(jobId)"
        />
      </div>
    </header>

    <Message v-if="jobs.errorMessage" severity="error" :closable="false">
      {{ jobs.errorMessage }}
    </Message>

    <section v-if="job" class="rounded-xl border border-slate-200 bg-white p-5">
      <dl class="grid gap-3 text-sm md:grid-cols-3">
        <div class="space-y-1">
          <dt class="magaz-muted text-xs">Магазин</dt>
          <dd>
            <RouterLink
              v-if="job.store_id"
              :to="{ name: 'store-detail', params: { id: job.store_id } }"
              class="text-sky-700 hover:underline"
            >
              {{ job.store_name ?? `#${job.store_id}` }}
            </RouterLink>
          </dd>
        </div>
        <div class="space-y-1">
          <dt class="magaz-muted text-xs">Автор</dt>
          <dd>{{ job.created_by ?? '—' }}</dd>
        </div>
        <div class="space-y-1">
          <dt class="magaz-muted text-xs">Попытки отправки</dt>
          <dd>{{ job.attempts }}</dd>
        </div>
        <div class="space-y-1">
          <dt class="magaz-muted text-xs">Создана</dt>
          <dd>{{ formatDate(job.created_at) }}</dd>
        </div>
        <div class="space-y-1">
          <dt class="magaz-muted text-xs">Начата</dt>
          <dd>{{ formatDate(job.started_at) }}</dd>
        </div>
        <div class="space-y-1">
          <dt class="magaz-muted text-xs">Завершена</dt>
          <dd>{{ formatDate(job.finished_at) }}</dd>
        </div>
        <div class="space-y-1 md:col-span-3">
          <dt class="magaz-muted text-xs">Лог-файлы на кассе</dt>
          <dd class="magaz-mono">{{ job.log_files.join(', ') || '—' }}</dd>
        </div>
      </dl>

      <Message v-if="job.last_error" severity="error" :closable="false" class="mt-4">
        {{ job.last_error }}
      </Message>

      <div class="mt-4">
        <h3 class="text-sm font-semibold">Параметры запроса к агенту</h3>
        <p class="magaz-muted text-xs">
          Пароль 1С здесь не хранится: сервер доставляет его отдельно (П-1).
        </p>
        <pre
          class="magaz-mono mt-2 max-h-48 overflow-auto rounded-lg bg-slate-900 p-3 text-xs text-slate-100"
          data-test="job-params"
          >{{ paramsJson }}</pre
        >
      </div>
    </section>

    <section v-if="job" class="rounded-xl border border-slate-200 bg-white p-5">
      <header class="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 class="font-semibold">Журнал задачи</h3>
          <p class="magaz-muted text-xs">
            Строки приходят из лога 1С и из ответов диспетчера (§4.4).
          </p>
        </div>
        <span class="magaz-muted text-xs">строк: {{ job.logs.length }}</span>
      </header>

      <ul class="mt-3 space-y-2" data-test="job-logs">
        <li
          v-for="entry in job.logs"
          :key="entry.id"
          class="rounded-lg border border-slate-200 p-3 text-sm"
        >
          <div class="flex items-center gap-2">
            <Tag
              :value="levelLabel(entry.level)"
              :severity="levelSeverity(entry.level)"
              data-test="log-level"
            />
            <span class="magaz-muted text-xs">{{ formatDate(entry.created_at) }}</span>
          </div>
          <p class="magaz-mono mt-2 whitespace-pre-wrap break-words">{{ entry.message }}</p>
        </li>
        <li
          v-if="!job.logs.length"
          class="magaz-muted rounded-lg border border-dashed border-slate-300 p-3 text-center text-sm"
          data-test="job-logs-empty"
        >
          Журнал пуст: задача ещё не отправлялась на кассу.
        </li>
      </ul>

      <div v-if="job.excerpt_preview" class="mt-4">
        <h4 class="text-sm font-semibold">Хвост лога операции</h4>
        <pre
          class="magaz-mono mt-2 max-h-72 overflow-auto rounded-lg bg-slate-900 p-3 text-xs text-slate-100"
          data-test="job-excerpt"
          >{{ job.excerpt_preview }}</pre
        >
      </div>
    </section>
  </div>
</template>
