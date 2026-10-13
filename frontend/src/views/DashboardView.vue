<script setup lang="ts">
/**
 * Главная (§11.1). Э2: плитки «Всего / Онлайн / Офлайн» берутся из
 * `/admin/system/info` (статус считает Health Monitor), раскрывающийся список
 * магазинов — из `/admin/stores`. Автообновление — polling каждые ~10 с.
 */
import { computed, onMounted, onUnmounted } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Tag from 'primevue/tag'

import StatusTile from '@/components/StatusTile.vue'
import { useStoresStore } from '@/stores/stores'
import { useSystemStore } from '@/stores/system'

const system = useSystemStore()
const stores = useStoresStore()

const counts = computed(() => system.counts)
const info = computed(() => system.info)
const onlineWindow = computed(() => counts.value?.online_window_sec ?? 45)

onMounted(() => stores.startPolling())
onUnmounted(() => stores.stopPolling())

async function refresh(): Promise<void> {
  await Promise.all([system.load(), stores.load()])
}

function formatTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU')
}
</script>

<template>
  <div class="space-y-6">
    <Message v-if="system.errorMessage" severity="error" :closable="false">
      {{ system.errorMessage }}
    </Message>

    <section class="grid grid-cols-2 gap-4 lg:grid-cols-3 xl:grid-cols-6">
      <StatusTile
        label="Всего магазинов"
        :value="counts?.stores_total ?? '—'"
        icon="pi pi-building"
        hint="заведено в дашборде"
      />
      <StatusTile
        label="Онлайн"
        :value="counts?.stores_online ?? '—'"
        tone="positive"
        icon="pi pi-check-circle"
        :hint="`ответ за ${onlineWindow} с`"
      />
      <StatusTile
        label="Офлайн"
        :value="counts?.stores_offline ?? '—'"
        tone="negative"
        icon="pi pi-times-circle"
        hint="нет ответа агента"
      />
      <StatusTile label="Устройства онлайн" value="—" icon="pi pi-wifi" hint="этап Э4Б (Д-1)" />
      <StatusTile label="Ждут ознакомления" value="—" icon="pi pi-bell" hint="этап Э5" />
      <StatusTile label="Задач в работе" value="—" icon="pi pi-spin pi-spinner" hint="этап Э4" />
    </section>

    <section class="rounded-xl border border-slate-200 bg-white p-5">
      <div class="flex items-center justify-between">
        <h2 class="font-semibold">Магазины</h2>
        <Button
          label="Обновить"
          icon="pi pi-refresh"
          size="small"
          text
          :loading="system.isLoading || stores.isLoading"
          data-test="refresh-dashboard"
          @click="refresh"
        />
      </div>

      <table class="magaz-table mt-4">
        <thead>
          <tr>
            <th class="w-8"></th>
            <th>Магазин</th>
            <th class="w-40">IP</th>
            <th class="w-40">Кластер</th>
            <th class="w-32">База</th>
            <th class="w-32">Версия 1С</th>
            <th class="w-24">Статус</th>
            <th class="w-44">Последняя активность</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="store in stores.items" :key="store.id" data-test="dashboard-store-row">
            <td colspan="8" class="p-0">
              <details class="group">
                <summary class="flex cursor-pointer items-center gap-3 py-2">
                  <span class="magaz-muted w-8 text-center text-xs">▸</span>
                  <span class="flex-1 font-medium">{{ store.name }}</span>
                  <span class="magaz-mono w-40">{{ store.ip_address }}</span>
                  <span class="w-40">{{ store.cluster_name }}</span>
                  <span class="w-32">{{ store.database_name }}</span>
                  <span class="magaz-mono w-32">{{ store.version_1c }}</span>
                  <span class="w-24">
                    <Tag
                      :value="store.is_online ? 'онлайн' : 'офлайн'"
                      :severity="store.is_online ? 'success' : 'danger'"
                    />
                  </span>
                  <span class="magaz-muted w-44 text-sm">{{ formatTime(store.last_seen_at) }}</span>
                </summary>

                <div class="magaz-muted grid gap-2 bg-slate-50 px-6 py-3 text-sm md:grid-cols-3">
                  <p>
                    Версия агента: <span class="magaz-mono">{{ store.agent_version ?? '—' }}</span>
                  </p>
                  <p>ОС кассы: {{ store.os_info ?? '—' }}</p>
                  <p>Порт агента: <span class="magaz-mono">{{ store.agent_port }}</span></p>
                  <p>Пользователь 1С: {{ store.user_name_1c }}</p>
                  <p>Пароль 1С: {{ store.password_set ? 'задан' : 'не задан' }}</p>
                  <p>
                    <RouterLink
                      :to="{ name: 'store-detail', params: { id: store.id } }"
                      class="text-blue-700"
                    >
                      Открыть карточку →
                    </RouterLink>
                  </p>
                </div>
              </details>
            </td>
          </tr>
          <tr v-if="!stores.items.length && !stores.isLoading">
            <td colspan="8" class="magaz-muted py-6 text-center">
              Магазинов нет. Заведите первый на странице «Магазины» — сервер выдаст токен агенту.
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="grid gap-4 lg:grid-cols-2">
      <div class="rounded-xl border border-slate-200 bg-white p-5">
        <div class="flex items-center justify-between">
          <h2 class="font-semibold">Состояние сервера</h2>
          <Button
            label="Обновить"
            icon="pi pi-refresh"
            size="small"
            text
            :loading="system.isLoading"
            data-test="refresh"
            @click="refresh"
          />
        </div>

        <dl class="mt-4 space-y-2 text-sm">
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">Версия приложения</dt>
            <dd class="font-medium">{{ info?.app_version ?? '—' }}</dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">СУБД</dt>
            <dd class="font-medium">{{ info?.database.kind ?? '—' }}</dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">Ревизия схемы (Alembic)</dt>
            <dd class="magaz-mono">{{ info?.database.schema_version ?? '—' }}</dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">Интерфейс (SPA)</dt>
            <dd class="font-medium">{{ system.isSpaBuilt ? 'собран' : 'не собран' }}</dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">Администраторов</dt>
            <dd class="font-medium">{{ counts?.admins_total ?? '—' }}</dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">Событий аудита</dt>
            <dd class="font-medium">{{ counts?.audit_events ?? '—' }}</dd>
          </div>
          <div class="flex justify-between gap-4">
            <dt class="magaz-muted">Настроек (§4.9)</dt>
            <dd class="font-medium">{{ info?.settings_keys ?? '—' }}</dd>
          </div>
        </dl>
      </div>

      <div class="rounded-xl border border-slate-200 bg-white p-5">
        <h2 class="font-semibold">Что появится дальше</h2>
        <ul class="mt-4 space-y-3 text-sm text-slate-700">
          <li class="flex gap-2">
            <span class="magaz-muted w-12 shrink-0 text-xs font-semibold">Э4</span>
            <span>Операции 1С: скачивание, проверка файлов, слияние, обновление БД, журнал задач.</span>
          </li>
          <li class="flex gap-2">
            <span class="magaz-muted w-12 shrink-0 text-xs font-semibold">Э4Б</span>
            <span>Оборудование подсети магазинов: камеры, свитчи, принтеры со статусами.</span>
          </li>
          <li class="flex gap-2">
            <span class="magaz-muted w-12 shrink-0 text-xs font-semibold">Э5</span>
            <span>Уведомления пользователям: уровни важности, «Ознакомлен»/«Отложить», отчёты.</span>
          </li>
        </ul>
      </div>
    </section>
  </div>
</template>
