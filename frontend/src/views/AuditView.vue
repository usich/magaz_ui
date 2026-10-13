<script setup lang="ts">
/**
 * Аудит действий администраторов (§4.10, §11.6). Работает в Э1: вход/выход и
 * изменение настроек уже пишутся в журнал; операции и магазины добавятся позже.
 */
import { onMounted, ref } from 'vue'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Tag from 'primevue/tag'

import { ApiError } from '@/api/client'
import { auditApi } from '@/api/endpoints'
import type { AuditEntry } from '@/api/types'

const entries = ref<AuditEntry[]>([])
const limit = ref(100)
const actorFilter = ref('')
const isLoading = ref(false)
const errorMessage = ref('')

const severityByAction: Record<string, string> = {
  admin_login: 'success',
  admin_logout: 'secondary',
  admin_login_failed: 'danger',
  settings_update: 'info',
}

onMounted(load)

async function load(): Promise<void> {
  isLoading.value = true
  try {
    entries.value = await auditApi.list({
      limit: limit.value,
      actor: actorFilter.value.trim() || undefined,
    })
    errorMessage.value = ''
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : 'Не удалось получить аудит'
  } finally {
    isLoading.value = false
  }
}

function formatTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU')
}

function detailsSummary(entry: AuditEntry): string {
  if (!entry.details) return '—'
  const keys = Object.keys(entry.details)
  return keys.length ? keys.join(', ') : '—'
}
</script>

<template>
  <div class="space-y-4">
    <Message v-if="errorMessage" severity="error" :closable="false">{{ errorMessage }}</Message>

    <div class="flex flex-wrap items-center gap-3">
      <InputText
        v-model="actorFilter"
        placeholder="Фильтр по логину администратора"
        class="w-72"
        @keyup.enter="load"
      />
      <select
        v-model.number="limit"
        class="rounded-lg border border-slate-300 px-3 py-2 text-sm"
        @change="load"
      >
        <option :value="50">50 записей</option>
        <option :value="100">100 записей</option>
        <option :value="200">200 записей</option>
      </select>
      <Button
        label="Обновить"
        icon="pi pi-refresh"
        size="small"
        :loading="isLoading"
        data-test="refresh-audit"
        @click="load"
      />
    </div>

    <div class="overflow-hidden rounded-xl border border-slate-200 bg-white">
      <table class="magaz-table">
        <thead>
          <tr>
            <th class="w-20">id</th>
            <th class="w-52">Время (UTC)</th>
            <th class="w-40">Администратор</th>
            <th class="w-52">Действие</th>
            <th class="w-40">Объект</th>
            <th class="w-40">IP</th>
            <th>Детали</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="entry in entries" :key="entry.id">
            <td class="magaz-muted">{{ entry.id }}</td>
            <td>{{ formatTime(entry.created_at) }}</td>
            <td>{{ entry.actor ?? '—' }}</td>
            <td>
              <Tag :value="entry.action" :severity="severityByAction[entry.action] ?? 'secondary'" />
            </td>
            <td class="magaz-muted">
              <span v-if="entry.entity">{{ entry.entity }}#{{ entry.entity_id ?? '—' }}</span>
              <span v-else>—</span>
            </td>
            <td class="magaz-mono">{{ entry.ip_address ?? '—' }}</td>
            <td class="magaz-muted">{{ detailsSummary(entry) }}</td>
          </tr>
          <tr v-if="!entries.length && !isLoading">
            <td colspan="7" class="magaz-muted py-6 text-center">Событий пока нет</td>
          </tr>
        </tbody>
      </table>
    </div>

    <p class="magaz-muted text-xs">
      В журнал никогда не пишутся значения паролей (I-7): только факт изменения учётных данных,
      настроек и входов.
    </p>
  </div>
</template>
