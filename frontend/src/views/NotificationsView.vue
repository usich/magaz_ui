<script setup lang="ts">
/**
 * Уведомления пользователям (§11.5, ФН3, Э5).
 *
 * Страница отвечает на два вопроса админа: «что я разослал» и «кто ещё не
 * ознакомился». Прогресс считает сервер по строкам `notification_receipts`
 * (§4.8), поэтому «X из Y» здесь — не догадка UI: числитель увеличивает только
 * явное «Ознакомлен», а «Отложить» (П-2) видно отдельной цифрой.
 *
 * Доставку выполняет диспетчер задачами `notify` (§11.4): выключенная касса
 * получит уведомление, когда вернётся в сеть, — поэтому статус «в работе» не
 * означает «ещё не отправлено».
 */
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'

import type {
  NotificationCategory,
  NotificationCategoryPayload,
  NotificationFilters,
  NotificationItem,
  NotificationSeverity,
} from '@/api/types'
import ConfirmDeleteDialog from '@/components/ConfirmDeleteDialog.vue'
import NotificationFormDialog from '@/components/NotificationFormDialog.vue'
import {
  SEVERITY_LABELS,
  STATE_LABELS,
  STATE_OPTIONS,
  bodyText,
  severityTone,
  targetTypeLabel,
  useNotificationsStore,
} from '@/stores/notifications'
import { useStoresStore } from '@/stores/stores'

const route = useRoute()
const toast = useToast()
const notifications = useNotificationsStore()
const stores = useStoresStore()

const isFormOpen = ref(false)

const storeOptions = computed(() => stores.items.map((item) => ({ id: item.id, name: item.name })))
const clusterOptions = computed(() =>
  Array.from(new Set(stores.items.map((item) => item.cluster_name ?? ''))).filter(Boolean).sort(),
)

const description = computed(
  () =>
    `В работе ${notifications.counts.active ?? 0} · завершено ${notifications.counts.done ?? 0} · ` +
    `отменено ${notifications.counts.canceled ?? 0}`,
)

onMounted(async () => {
  // Плитка главной ведёт сюда с фильтром «в работе»: `?status=active` (§11.1)
  const status = route.query.status
  if (typeof status === 'string' && STATE_OPTIONS.includes(status as 'active')) {
    notifications.status = status as NotificationFilters['status']
  }
  if (!stores.items.length) await stores.load()
  await notifications.load()
  // 1.22, §11.5: категории нужны разделу «Категории» — читаем сразу с отключёнными
  if (!notifications.allCategories.length) await notifications.loadCategories(true)
})

/** Вкладка важности: меняет фильтр списка и перечитывает выборку (§11.5, Э9). */
async function selectSeverity(value: NotificationSeverity | 'all'): Promise<void> {
  if (notifications.severity === value) return
  notifications.severity = value
  await notifications.load()
}

async function cancel(item: NotificationItem): Promise<void> {
  if (!(await notifications.cancel(item.id))) {
    toast.add({
      severity: 'error',
      summary: 'Рассылка не отменена',
      detail: notifications.errorMessage,
      life: 6000,
    })
    return
  }
  toast.add({
    severity: 'success',
    summary: 'Рассылка остановлена',
    detail: item.title,
    life: 4000,
  })
}

/** Успех создания: сразу видно, сколько магазинов получит уведомление (§11.5). */
function onSaved(count: number): void {
  toast.add({
    severity: 'success',
    summary: 'Уведомление разослано',
    detail: `Магазинов: ${count}. Доставку выполняет диспетчер (§11.4)`,
    life: 5000,
  })
}

/** Э10: «Удалить» — уведомление уходит вместе с отчётом, поэтому подтверждение. */
const pendingDelete = ref<NotificationItem | null>(null)
const isDeleting = ref(false)

function askDelete(item: NotificationItem): void {
  pendingDelete.value = item
}

async function confirmDelete(): Promise<void> {
  const item = pendingDelete.value
  if (!item) return
  isDeleting.value = true
  try {
    if (!(await notifications.remove(item.id))) {
      toast.add({
        severity: 'error',
        summary: 'Уведомление не удалено',
        detail: notifications.errorMessage,
        life: 6000,
      })
      return
    }
    pendingDelete.value = null
    toast.add({
      severity: 'success',
      summary: 'Уведомление удалено',
      detail: `${item.title} — задачи рассылки закрыты, картинки удалены`,
      life: 5000,
    })
  } finally {
    isDeleting.value = false
  }
}

function formatTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU')
}

// --- Категории уведомлений (спека 1.22, §11.5) ------------------------------

/** Открыта ли форма категории (создание или правка). */
const categoryFormOpen = ref(false)
/** Id редактируемой категории; `null` — создание новой. */
const editingCategoryId = ref<number | null>(null)
const categoryName = ref('')
const categoryColor = ref<string>((notifications.CATEGORY_COLORS[0] as string) ?? '#64748b')
const isCategorySaving = ref(false)

function openCreateCategory(): void {
  editingCategoryId.value = null
  categoryName.value = ''
  categoryColor.value = (notifications.CATEGORY_COLORS[0] as string) ?? '#64748b'
  categoryFormOpen.value = true
}

function openEditCategory(category: NotificationCategory): void {
  editingCategoryId.value = category.id
  categoryName.value = category.name
  categoryColor.value = category.color
  categoryFormOpen.value = true
}

/** Создать или сохранить категорию: имя обязательно, цвет — из палитры сервера (§11.5). */
async function saveCategory(): Promise<void> {
  const name = categoryName.value.trim()
  if (!name) {
    toast.add({ severity: 'warn', summary: 'Имя категории не может быть пустым', life: 4000 })
    return
  }
  isCategorySaving.value = true
  try {
    const payload: NotificationCategoryPayload = { name, color: categoryColor.value }
    const ok =
      editingCategoryId.value === null
        ? await notifications.createCategory(payload)
        : await notifications.updateCategory(editingCategoryId.value, payload)
    if (!ok) {
      toast.add({
        severity: 'error',
        summary: editingCategoryId.value === null ? 'Категория не создана' : 'Категория не изменена',
        detail: notifications.errorMessage,
        life: 6000,
      })
      return
    }
    categoryFormOpen.value = false
    toast.add({
      severity: 'success',
      summary: editingCategoryId.value === null ? 'Категория создана' : 'Категория изменена',
      detail: name,
      life: 4000,
    })
  } finally {
    isCategorySaving.value = false
  }
}

/** Отключить категорию (мягкое удаление) или включить отключённую обратно (§11.5). */
async function toggleCategory(category: NotificationCategory): Promise<void> {
  const payload: NotificationCategoryPayload = {
    name: category.name,
    color: category.color,
    is_active: !category.is_active,
  }
  const ok = await notifications.updateCategory(category.id, payload)
  if (!ok) {
    toast.add({
      severity: 'error',
      summary: 'Категория не изменена',
      detail: notifications.errorMessage,
      life: 6000,
    })
  }
}
</script>

<template>
  <div class="space-y-6">
    <header class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h2 class="text-xl font-semibold">Уведомления</h2>
        <p class="magaz-muted text-sm">
          Сообщения пользователям магазинов: уровни важности, адресация и отчёт «ознакомлены X из Y»
          по каждой сессии Windows.
        </p>
        <p class="magaz-muted text-xs" data-test="notifications-summary">{{ description }}</p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <span v-if="notifications.lastLoadedAt" class="magaz-muted text-xs">
          обновлено {{ notifications.lastLoadedAt.toLocaleTimeString('ru-RU') }}
        </span>
        <Button
          label="Обновить"
          icon="pi pi-refresh"
          size="small"
          text
          :loading="notifications.isLoading"
          data-test="notifications-refresh"
          @click="notifications.load()"
        />
        <Button
          label="Создать уведомление"
          icon="pi pi-bell"
          size="small"
          data-test="notifications-create"
          @click="isFormOpen = true"
        />
      </div>
    </header>

    <!-- Вкладки по важности (Э9, §11.5): счётчики по всем уведомлениям, а не по фильтру
         — нажатие вкладки меняет только выборку, цифры на соседних вкладках не «плывут». -->
    <nav class="flex flex-wrap gap-2" data-test="notifications-tabs">
      <button
        v-for="tab in notifications.severityTabs"
        :key="tab.value"
        type="button"
        class="rounded-lg border px-3 py-2 text-sm"
        :class="
          notifications.severity === tab.value
            ? 'border-sky-500 bg-sky-50 font-medium text-sky-800'
            : 'border-slate-200 bg-white hover:border-slate-300'
        "
        :data-test="`notifications-tab-${tab.value}`"
        @click="selectSeverity(tab.value)"
      >
        {{ tab.label }}
        <span class="magaz-muted">({{ tab.count }})</span>
        <span v-if="tab.unread" class="magaz-muted text-xs">· ждут ознакомления {{ tab.unread }}</span>
      </button>
    </nav>

    <Message
      v-if="notifications.errorMessage"
      severity="error"
      :closable="false"
      data-test="notifications-error"
    >
      {{ notifications.errorMessage }}
    </Message>

    <section class="grid gap-4 md:grid-cols-3">
      <div class="rounded-xl border border-slate-200 bg-white p-4">
        <div class="magaz-muted text-xs">Ждут ознакомления</div>
        <div class="text-2xl font-semibold" data-test="notifications-pending">
          {{ notifications.pendingReceipts }}
        </div>
        <div class="magaz-muted text-xs">сессии, которые ещё не нажали «Ознакомлен»</div>
      </div>
      <div class="rounded-xl border border-slate-200 bg-white p-4">
        <div class="magaz-muted text-xs">Возврат отложенного</div>
        <div class="text-2xl font-semibold" data-test="notifications-snooze">
          {{ notifications.snoozeMin }} мин
        </div>
        <div class="magaz-muted text-xs">настройка сервера `notification_snooze_min` (§4.9, П-2)</div>
      </div>
      <div class="rounded-xl border border-slate-200 bg-white p-4">
        <div class="magaz-muted text-xs">Рассылок в списке</div>
        <div class="text-2xl font-semibold" data-test="notifications-total">
          {{ notifications.total }}
        </div>
        <div class="magaz-muted text-xs">с учётом выбранных фильтров</div>
      </div>
    </section>

    <div class="flex flex-wrap items-center gap-3">
      <InputText
        v-model="notifications.search"
        placeholder="Поиск по заголовку и тексту"
        class="w-64"
        data-test="notifications-search"
        @keyup.enter="notifications.load()"
      />
      <select
        v-model="notifications.status"
        class="rounded-lg border border-slate-300 px-3 py-2 text-sm"
        data-test="notifications-status-filter"
        @change="notifications.load()"
      >
        <option v-for="item in STATE_OPTIONS" :key="item" :value="item">
          {{ item === 'all' ? 'Все статусы' : STATE_LABELS[item] }}
        </option>
      </select>
      <select
        v-model="notifications.storeId"
        class="rounded-lg border border-slate-300 px-3 py-2 text-sm"
        data-test="notifications-store-filter"
        @change="notifications.load()"
      >
        <option :value="null">Все магазины</option>
        <option v-for="item in storeOptions" :key="item.id" :value="item.id">{{ item.name }}</option>
      </select>
    </div>

    <section class="overflow-hidden rounded-xl border border-slate-200 bg-white">
      <table class="magaz-table">
        <thead>
          <tr>
            <th class="w-16">id</th>
            <th>Уведомление</th>
            <th class="w-28">Уровень</th>
            <th>Охват</th>
            <th class="w-56">Ознакомлены</th>
            <th class="w-28">Статус</th>
            <th class="w-44">Создано</th>
            <th class="w-56">Действия</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in notifications.items"
            :key="row.id"
            :data-test="`notification-row-${row.id}`"
          >
            <td class="magaz-muted">{{ row.id }}</td>
            <td>
              <RouterLink
                :to="{ name: 'notification-detail', params: { id: row.id } }"
                class="font-medium text-sky-700 hover:underline"
              >
                {{ row.title }}
              </RouterLink>
              <span
                v-if="row.category"
                class="ml-2 inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium text-white align-middle"
                :style="{ backgroundColor: row.category.color }"
                :data-test="`notification-category-${row.id}`"
              >
                {{ row.category.name }}
              </span>
              <div class="magaz-muted text-xs">{{ bodyText(row) }}</div>
              <div
                v-if="row.body_format === 'html' || row.images.length || row.delete_after_read"
                class="magaz-muted text-[11px]"
                :data-test="`notification-rich-${row.id}`"
              >
                <span v-if="row.body_format === 'html'">с разметкой</span>
                <span v-if="row.images.length"> · картинок {{ row.images.length }}</span>
                <span v-if="row.delete_after_read"> · удаляется после прочтения</span>
              </div>
            </td>
            <td>
              <Tag
                :value="SEVERITY_LABELS[row.severity] ?? row.severity"
                :severity="severityTone(row.severity)"
                :data-test="`notification-severity-${row.id}`"
              />
            </td>
            <td>
              <div class="text-sm">{{ targetTypeLabel(row) }}</div>
              <div class="magaz-muted text-xs">
                магазинов {{ row.totals.stores }} · доставлено {{ row.totals.stores_delivered }}
              </div>
            </td>
            <td>
              <div class="text-sm" :data-test="`notification-progress-${row.id}`">
                {{ row.totals.acknowledged }} из {{ row.totals.receipts }}
                <span v-if="row.totals.snoozed" class="magaz-muted">
                  · отложили {{ row.totals.snoozed }}
                </span>
                <span v-if="row.totals.expired" class="magaz-muted">
                  · просрочено {{ row.totals.expired }}
                </span>
              </div>
              <div class="mt-1 h-2 w-full rounded bg-slate-100">
                <div class="h-2 rounded bg-emerald-500" :style="{ width: `${row.totals.percent}%` }"></div>
              </div>
              <div class="magaz-muted text-xs">{{ row.totals.percent }}%</div>
            </td>
            <td>
              <Tag
                :value="STATE_LABELS[row.state] ?? row.state"
                :data-test="`notification-state-${row.id}`"
              />
            </td>
            <td>
              <div class="text-sm">{{ formatTime(row.created_at) }}</div>
              <div class="magaz-muted text-xs">
                {{ row.created_by ?? '—' }}
                <span v-if="row.requires_ack"> · требует «Ознакомлен»</span>
              </div>
            </td>
            <td>
              <div class="flex flex-wrap items-center gap-2">
                <RouterLink
                  :to="{ name: 'notification-detail', params: { id: row.id } }"
                  class="text-sm text-sky-700 hover:underline"
                  :data-test="`notification-report-link-${row.id}`"
                >
                  Отчёт
                </RouterLink>
                <a
                  :href="notifications.csvUrl(row.id)"
                  class="text-sm text-sky-700 hover:underline"
                  :data-test="`notification-csv-${row.id}`"
                >
                  CSV
                </a>
                <Button
                  v-if="row.is_active"
                  label="Отменить"
                  size="small"
                  text
                  severity="danger"
                  :data-test="`notification-cancel-${row.id}`"
                  @click="cancel(row)"
                />
                <Button
                  label="Удалить"
                  size="small"
                  text
                  severity="danger"
                  :data-test="`notification-delete-${row.id}`"
                  @click="askDelete(row)"
                />
              </div>
            </td>
          </tr>
          <tr v-if="!notifications.items.length">
            <td colspan="8" class="magaz-muted py-6 text-center" data-test="notifications-empty">
              Уведомлений пока нет: создайте первое — оно уйдёт на кассы через диспетчер (§11.4).
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- Категории (спека 1.22, §11.5): справочник, по которому касса группирует
         уведомления. Здесь категории заводят, переименовывают и отключают —
         активные предлагает форма «Создать уведомление». -->
    <section class="rounded-xl border border-slate-200 bg-white p-5" data-test="notifications-categories">
      <header class="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 class="text-lg font-semibold">Категории</h3>
          <p class="magaz-muted text-sm">
            По ним касса группирует список уведомлений (§7.4). Цвет плашки — из палитры сервера (§11.5).
          </p>
        </div>
        <Button
          label="Добавить категорию"
          icon="pi pi-plus"
          size="small"
          data-test="category-add"
          @click="openCreateCategory"
        />
      </header>

      <form
        v-if="categoryFormOpen"
        class="mt-4 flex flex-wrap items-center gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3"
        data-test="category-form"
        @submit.prevent="saveCategory"
      >
        <InputText
          v-model="categoryName"
          placeholder="Имя категории, например «Продажи»"
          class="w-56"
          :pt="{ root: { 'data-test': 'category-name' } }"
        />
        <div class="flex items-center gap-1">
          <button
            v-for="color in notifications.CATEGORY_COLORS"
            :key="color"
            type="button"
            class="h-7 w-7 rounded-full border-2"
            :class="categoryColor === color ? 'border-sky-600' : 'border-transparent'"
            :style="{ backgroundColor: color }"
            :aria-label="color"
            :data-test="`category-color-${color.replace('#', '')}`"
            @click="categoryColor = color"
          />
        </div>
        <Button label="Сохранить" size="small" :loading="isCategorySaving" data-test="category-save" type="submit" />
        <Button
          label="Отмена"
          size="small"
          text
          data-test="category-cancel"
          type="button"
          @click="categoryFormOpen = false"
        />
      </form>

      <ul class="mt-4 space-y-2">
        <li
          v-for="category in notifications.allCategories"
          :key="category.id"
          class="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-200 px-3 py-2"
          :data-test="`category-row-${category.id}`"
        >
          <div class="flex items-center gap-2">
            <span
              class="inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium text-white"
              :style="{ backgroundColor: category.color }"
            >
              {{ category.name }}
            </span>
            <span v-if="!category.is_active" class="magaz-muted text-xs">отключена</span>
          </div>
          <div class="flex items-center gap-1">
            <Button
              label="Изменить"
              size="small"
              text
              :data-test="`category-edit-${category.id}`"
              @click="openEditCategory(category)"
            />
            <Button
              :label="category.is_active ? 'Отключить' : 'Включить'"
              size="small"
              text
              severity="secondary"
              :data-test="`category-toggle-${category.id}`"
              @click="toggleCategory(category)"
            />
          </div>
        </li>
        <li
          v-if="!notifications.allCategories.length"
          class="magaz-muted text-sm"
          data-test="categories-empty"
        >
          Категорий пока нет: создайте первую — касса сгруппирует уведомления по ней (§7.4).
        </li>
      </ul>
    </section>

    <NotificationFormDialog
      v-model:visible="isFormOpen"
      :store-options="storeOptions"
      :cluster-options="clusterOptions"
      @saved="onSaved($event.stores)"
    />

    <ConfirmDeleteDialog
      :visible="pendingDelete !== null"
      header="Удалить уведомление?"
      :message="`«${pendingDelete?.title ?? ''}» исчезнет из списка вместе с отчётом «кто ознакомился».`"
      :details="[
        'Отметки «ознакомлен», «отложено» и адресаты магазинов будут удалены',
        'Картинки тела удаляются с сервера вместе с файлами',
        'Незавершённые задачи рассылки закроются: офлайн-кассы его не получат',
        'Окна, уже показанные сотрудникам, отозвать нельзя — как и при отмене (§7.4)',
      ]"
      :loading="isDeleting"
      @update:visible="(value: boolean) => { if (!value) pendingDelete = null }"
      @confirm="confirmDelete"
    />
  </div>
</template>
