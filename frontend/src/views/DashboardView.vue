<script setup lang="ts">
/**
 * Главная (§11.1). Э2: плитки «Всего / Онлайн / Офлайн» берутся из
 * `/admin/system/info` (статус считает Health Monitor), раскрывающийся список
 * магазинов — из `/admin/stores`. Э4: плитка «Задач в работе» — это
 * `counts.jobs_active` (задачи в очереди и в работе у диспетчера).
 * Автообновление — polling каждые ~10 с.
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

/** Э4Б: плитка «Устройства онлайн / всего» — те же числа, что на `/devices` (§11.7). */
const devicesValue = computed(() => {
  const total = counts.value?.devices_total ?? 0
  const online = counts.value?.devices_online ?? 0
  return `${online} / ${total}`
})
const devicesHint = computed(() => {
  const window = counts.value?.devices_window_sec ?? 180
  return `окно ${window} с · офлайн и без данных — по клику`
})

/** Э5: интервал возврата отложенного уведомления (§4.9, П-2). */
const snoozeMin = computed(() => counts.value?.notifications_snooze_min ?? 15)

/**
 * Д-14: плитка магазинов считает **магазины в работе**.
 *
 * «Отключить» на странице магазинов — мягкое удаление (§5.2.2): строка остаётся
 * в таблице ради истории и отчётов, поэтому `stores_total` (число строк) больше
 * числа работающих. Плитка берёт `stores_active`, а расхождение показывает
 * подсказкой — иначе главная «держала» 2 магазина при одном рабочем.
 */
const inactiveStores = computed(() => counts.value?.stores_inactive ?? 0)
const storesHint = computed(() =>
  inactiveStores.value ? `активных · отключено ${inactiveStores.value}` : 'активных магазинов',
)

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
        label="Магазинов в работе"
        :value="counts?.stores_active ?? '—'"
        icon="pi pi-building"
        :hint="storesHint"
        data-test="tile-stores"
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
      <RouterLink
        :to="{ name: 'devices', query: { status: 'offline' } }"
        class="block"
        data-test="tile-devices"
        title="Оборудование подсети: что офлайн и что без данных"
      >
        <StatusTile
          label="Устройства онлайн"
          :value="devicesValue"
          icon="pi pi-wifi"
          :hint="devicesHint"
        />
      </RouterLink>
      <RouterLink
        :to="{ name: 'notifications', query: { status: 'active' } }"
        class="block"
        data-test="tile-notifications"
        title="Уведомления: кто ещё не нажал «Ознакомлен»"
      >
        <StatusTile
          label="Ждут ознакомления"
          :value="counts?.notifications_pending ?? '—'"
          icon="pi pi-bell"
          :hint="`отложенное возвращается через ${snoozeMin} мин`"
        />
      </RouterLink>
      <StatusTile
        label="Задач в работе"
        :value="counts?.jobs_active ?? '—'"
        icon="pi pi-spin pi-spinner"
        hint="очередь и выполнение"
      />
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
            <span class="magaz-muted w-12 shrink-0 text-xs font-semibold">Э7</span>
            <span>Приёмка на стенде заказчика: живой 1С, `msg.exe`, службы под LocalSystem.</span>
          </li>
          <li class="flex gap-2">
            <span class="magaz-muted w-12 shrink-0 text-xs font-semibold">Э9</span>
            <span>
              Уведомления 2.0: редактор с форматированием и картинками, окно в углу экрана на
              кассе, вкладки по важности со счётчиками, удаление после прочтения.
            </span>
          </li>
        </ul>
      </div>
    </section>
  </div>
</template>
