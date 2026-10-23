<script setup lang="ts">
/**
 * Главная (§11.1). Э2: плитки «Всего / Онлайн / Офлайн» берутся из
 * `/admin/system/info` (статус считает Health Monitor), раскрывающийся список
 * магазинов — из `/admin/stores`. Э4: плитка «Задач в работе» — это
 * `counts.jobs_active` (задачи в очереди и в работе у диспетчера).
 * Автообновление — polling каждые ~10 с.
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import Message from 'primevue/message'
import Tag from 'primevue/tag'

import StatusTile from '@/components/StatusTile.vue'
import { useBackupStore } from '@/stores/backup'
import { useStoresStore } from '@/stores/stores'
import { useSystemStore } from '@/stores/system'

const system = useSystemStore()
const stores = useStoresStore()
const backups = useBackupStore()
/** Какой список бэкапов открыт в модалке: магазины или локальные базы (`null` — закрыта). */
const backupListKind = ref<'stores' | 'local-bases' | null>(null)
const isBackupListOpen = ref(false)

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

onMounted(() => {
  stores.startPolling()
  void backups.load()
})
onUnmounted(() => stores.stopPolling())

async function refresh(): Promise<void> {
  await Promise.all([system.load(), stores.load(), backups.load()])
}

function backupLabel(cat: { total: number; ok: number }): string {
  return cat.total ? `есть ${cat.ok} из ${cat.total}` : 'не настроены'
}

function backupBadge(status: string): string {
  if (status === 'ok') return 'есть'
  if (status === 'missing') return 'нет'
  return 'не проверено'
}

/** Следующая автоматическая проверка бэкапов (Д-21, показываем локальное время). */
const nextCheckLabel = computed(() => {
  const iso = backups.status.next_check_iso
  if (!iso) return ''
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('ru-RU')
})

/** Тревога о пропавших бэкапах (Д-21, без рассылки на кассы): текст или ''. */
const backupAlertText = computed(() => {
  const storeMissing = backups.status.stores.missing
  const baseMissing = backups.status.local_bases.missing
  const parts: string[] = []
  if (storeMissing > 0) parts.push(`магазинов — ${storeMissing}`)
  if (baseMissing > 0) parts.push(`баз — ${baseMissing}`)
  return parts.length ? `Нет бэкапа у: ${parts.join(', ')}` : ''
})


function openBackupList(kind: 'stores' | 'local-bases'): void {
  backupListKind.value = kind
  isBackupListOpen.value = true
}

/** Ручная проверка не найденных бэкапов (без лишних запросов по найденным). */
async function runBackupCheck(): Promise<void> {
  await backups.checkMissing()
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

    <!-- Д-21: блок «Бэкапы» на главной -->
    <section class="rounded-xl border border-slate-200 bg-white p-5" data-test="backup-dashboard">
      <header class="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 class="font-semibold">Бэкапы 1С</h2>
          <p class="magaz-muted text-xs">Проверка наличия бэкапов (Backup Monitor, Д-21)</p>
        </div>
        <Button
          label="Проверить не найденные"
          icon="pi pi-refresh"
          size="small"
          text
          :loading="backups.isChecking"
          :disabled="backups.isChecking"
          data-test="backup-check-missing"
          @click="runBackupCheck"
        />
      </header>

      <!-- Д-21: тревога о пропавших бэкапах (без рассылки на кассы) -->
      <div
        v-if="backupAlertText"
        class="mt-3 flex items-center gap-2 rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700"
        data-test="backup-alert"
      >
        <i class="pi pi-exclamation-triangle" />
        <span>{{ backupAlertText }}</span>
      </div>
      <p v-if="nextCheckLabel" class="magaz-muted mt-3 text-xs" data-test="backup-next-check">
        Следующая автопроверка: {{ nextCheckLabel }}
      </p>


      <div class="mt-4 grid gap-4 sm:grid-cols-2">
        <button
          type="button"
          class="text-left"
          data-test="backup-stores"
          @click="openBackupList('stores')"
        >
          <div class="rounded-xl border border-slate-200 p-4 hover:border-blue-300">
            <div class="flex items-center justify-between">
              <span class="font-medium">Магазины</span>
              <span
                :class="backups.status.stores.missing > 0 ? 'text-rose-600' : 'text-emerald-600'"
                class="text-sm font-semibold"
              >
                {{ backupLabel(backups.status.stores) }}
              </span>
            </div>
            <p class="magaz-muted mt-1 text-xs">
              проверено {{ backups.status.stores.checked }} · нет бэкапа у
              {{ backups.status.stores.missing }}
            </p>
          </div>
        </button>

        <button
          type="button"
          class="text-left"
          data-test="backup-local-bases"
          @click="openBackupList('local-bases')"
        >
          <div class="rounded-xl border border-slate-200 p-4 hover:border-blue-300">
            <div class="flex items-center justify-between">
              <span class="font-medium">Локальные базы</span>
              <span
                :class="backups.status.local_bases.missing > 0 ? 'text-rose-600' : 'text-emerald-600'"
                class="text-sm font-semibold"
              >
                {{ backupLabel(backups.status.local_bases) }}
              </span>
            </div>
            <p class="magaz-muted mt-1 text-xs">
              проверено {{ backups.status.local_bases.checked }} · нет бэкапа у
              {{ backups.status.local_bases.missing }}
            </p>
          </div>
        </button>
      </div>
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
          <tr class="magaz-muted text-left text-xs font-medium">
            <th class="w-8"></th>
            <th class="whitespace-nowrap px-2">Магазин</th>
            <th class="w-40 whitespace-nowrap px-2">IP</th>
            <th class="w-40 whitespace-nowrap px-2">Кластер</th>
            <th class="w-32 whitespace-nowrap px-2">База</th>
            <th class="w-32 whitespace-nowrap px-2">Версия 1С</th>
            <th class="w-24 whitespace-nowrap px-2">Статус</th>
            <th class="w-44 whitespace-nowrap px-2">Последняя активность</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="store in stores.items" :key="store.id" data-test="dashboard-store-row">
            <td colspan="8" class="p-0">
              <details class="group">
                <summary class="flex cursor-pointer items-center py-2">
                  <span class="magaz-muted w-8 text-center text-xs">▸</span>
                  <span class="flex-1 truncate px-2 font-medium">{{ store.name }}</span>
                  <span class="magaz-mono w-40 truncate px-2">{{ store.ip_address }}</span>
                  <span class="w-40 truncate px-2">{{ store.cluster_name }}</span>
                  <span class="w-32 truncate px-2">{{ store.database_name }}</span>
                  <span class="magaz-mono w-32 truncate px-2">{{ store.version_1c }}</span>
                  <span class="w-24 px-2">
                    <Tag
                      :value="store.is_online ? 'онлайн' : 'офлайн'"
                      :severity="store.is_online ? 'success' : 'danger'"
                    />
                  </span>
                  <span class="magaz-muted w-44 whitespace-nowrap px-2 text-sm">{{ formatTime(store.last_seen_at) }}</span>
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


    <Dialog
      v-model:visible="isBackupListOpen"
      :header="backupListKind === 'stores' ? 'Список бэкапов — магазины' : 'Список бэкапов — локальные базы'"
      modal
      :style="{ width: '720px' }"
      data-test="backup-list-dialog"
    >
      <div
        v-if="backupListKind === 'stores' ? !backups.storeList.length : !backups.localBaseList.length"
        class="magaz-muted py-6 text-center text-sm"
      >
        Проверка бэкапов не настроена {{ backupListKind === 'stores' ? 'ни у одного магазина' : 'ни у одной локальной базы' }}.
      </div>
      <table v-else class="w-full text-sm">
        <thead>
          <tr class="magaz-muted border-b border-slate-200 text-left text-xs">
            <th class="py-2 pr-3 font-medium">Название</th>
            <th class="py-2 pr-3 font-medium">Статус</th>
            <th class="py-2 pr-3 font-medium">За дату</th>
            <th class="py-2 pr-3 font-medium">Ошибка</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in backupListKind === 'stores' ? backups.storeList : backups.localBaseList"
            :key="item.id"
            class="border-b border-slate-100"
          >
            <td class="py-2 pr-3 font-medium">{{ item.name }}</td>
            <td class="py-2 pr-3">
              <Tag
                :value="backupBadge(item.backup_status)"
                :severity="item.backup_status === 'ok' ? 'success' : item.backup_status === 'missing' ? 'danger' : 'secondary'"
              />
            </td>
            <td class="py-2 pr-3 whitespace-nowrap">
              {{ item.backup_checked_date ?? '—' }}
              <span class="magaz-muted text-xs">{{ item.backup_check_yesterday ? '(вчера)' : '(сегодня)' }}</span>
            </td>
            <td class="magaz-muted max-w-xs truncate py-2 pr-3" :title="item.backup_error ?? ''">
              {{ item.backup_error ?? '—' }}
            </td>
          </tr>
        </tbody>
      </table>
      <template #footer>
        <Button label="Закрыть" size="small" text @click="isBackupListOpen = false" />
      </template>
    </Dialog>


    <section>
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
    </section>
  </div>
</template>
