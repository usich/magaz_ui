<script setup lang="ts">
/**
 * Отчёт по уведомлению (§11.5, ФН3, Э5): магазины → сессии.
 *
 * «X из Y» здесь считается по строкам `notification_receipts`, а не по числу
 * магазинов: адресат — учётная запись Windows (П-6), поэтому в отчёте видно,
 * какой именно пользователь прочитал уведомление, а какой «отложил» его (П-2).
 * Отложенная строка выделяется отдельно: она не входит в числитель.
 *
 * CSV-выгрузка берётся ссылкой на сервер: файл скачивает браузер вместе с cookie
 * сессии, поэтому «экспорт» не зависит от того, как отрисован отчёт.
 */
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'

import type { NotificationReceiptRow, NotificationStoreReport } from '@/api/types'
import ConfirmDeleteDialog from '@/components/ConfirmDeleteDialog.vue'
import NotificationBody from '@/components/NotificationBody.vue'
import {
  RECEIPT_LABELS,
  SEVERITY_LABELS,
  STATE_LABELS,
  notificationImageUrl,
  receiptLabel,
  receiptTone,
  targetTypeLabel,
  useNotificationsStore,
} from '@/stores/notifications'

const props = defineProps<{ id: string }>()

const notifications = useNotificationsStore()
const toast = useToast()
const router = useRouter()

const notificationId = computed(() => Number(props.id))
const report = computed(() => notifications.report)
const item = computed(() => report.value?.notification ?? null)

onMounted(() => notifications.loadReport(notificationId.value))

/** «Отменить»: показы прекращаются, отчёт по уже нажатым сохраняется (§11.5). */
async function cancel(): Promise<void> {
  if (!item.value) return
  if (!(await notifications.cancel(item.value.id))) {
    toast.add({
      severity: 'error',
      summary: 'Рассылка не отменена',
      detail: notifications.errorMessage,
      life: 6000,
    })
    return
  }
  toast.add({ severity: 'success', summary: 'Рассылка остановлена', life: 4000 })
}

/** Строка отчёта по сессии: отложенное видно отдельной строкой (§11.5, П-2). */
function sessionRows(store: NotificationStoreReport): NotificationReceiptRow[] {
  return store.receipts
}

/** Э10: «Удалить уведомление» — после удаления отчёт показывать нечего. */
const pendingDelete = ref(false)
const isDeleting = ref(false)

async function confirmDelete(): Promise<void> {
  if (!item.value) return
  isDeleting.value = true
  try {
    if (!(await notifications.remove(item.value.id))) {
      toast.add({
        severity: 'error',
        summary: 'Уведомление не удалено',
        detail: notifications.errorMessage,
        life: 6000,
      })
      return
    }
    pendingDelete.value = false
    toast.add({
      severity: 'success',
      summary: 'Уведомление удалено',
      detail: 'Отчёт и картинки удалены вместе с ним',
      life: 5000,
    })
    await router.push({ name: 'notifications' })
  } finally {
    isDeleting.value = false
  }
}

/** Э10: удаление одной картинки — метка уходит из тела, файл — с сервера (§4.6). */
const pendingImage = ref<string | null>(null)
const isImageBusy = ref(false)

async function confirmImageDelete(): Promise<void> {
  const name = pendingImage.value
  if (!name || !item.value) return
  isImageBusy.value = true
  try {
    if (!(await notifications.removeImage(item.value.id, name))) {
      toast.add({
        severity: 'error',
        summary: 'Картинка не удалена',
        detail: notifications.errorMessage,
        life: 6000,
      })
      return
    }
    pendingImage.value = null
    toast.add({ severity: 'success', summary: 'Картинка удалена', detail: name, life: 4000 })
  } finally {
    isImageBusy.value = false
  }
}

function formatTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU')
}
</script>

<template>
  <div class="space-y-6">
    <header class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <RouterLink
          :to="{ name: 'notifications' }"
          class="magaz-muted text-xs hover:underline"
          data-test="notification-back"
        >
          ← к списку уведомлений
        </RouterLink>
        <h2 class="text-xl font-semibold" data-test="notification-title">
          {{ item?.title ?? 'Отчёт по уведомлению' }}
        </h2>
        <NotificationBody
          v-if="item"
          class="mt-2 max-w-3xl"
          :body="item.body"
          :body-format="item.body_format"
          :notification-id="item.id"
        />
        <div v-if="item" class="magaz-muted text-xs">
          {{ SEVERITY_LABELS[item.severity] ?? item.severity }} · {{ targetTypeLabel(item) }} ·
          создано {{ formatTime(item.created_at) }} автором {{ item.created_by ?? '—' }}
          <span v-if="item.images.length"> · картинок {{ item.images.length }}</span>
          <span v-if="item.delete_after_read"> · удаляется после прочтения на кассе</span>
        </div>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <Tag v-if="report" :value="STATE_LABELS[report.state] ?? report.state" />
        <a
          :href="notifications.csvUrl(notificationId)"
          class="text-sm text-sky-700 hover:underline"
          data-test="notification-detail-csv"
        >
          Экспорт CSV
        </a>
        <Button
          v-if="item?.is_active"
          label="Отменить рассылку"
          icon="pi pi-times"
          size="small"
          severity="danger"
          text
          data-test="notification-detail-cancel"
          @click="cancel"
        />
        <Button
          v-if="item"
          label="Удалить уведомление"
          icon="pi pi-trash"
          size="small"
          severity="danger"
          text
          data-test="notification-detail-delete"
          @click="pendingDelete = true"
        />
      </div>
    </header>

    <!-- Э10: картинки тела — реальные файлы на сервере, их видно и можно убрать (§4.6) -->
    <section
      v-if="item?.images.length"
      class="rounded-xl border border-slate-200 bg-white p-4"
      data-test="notification-detail-images"
    >
      <h3 class="text-sm font-semibold">Картинки тела ({{ item.images.length }})</h3>
      <p class="magaz-muted text-xs">
        Касса скачивает их в свой кэш при доставке: удаление действует на сервере и на то, что ещё
        не отправлено (§7.4).
      </p>
      <ul class="mt-3 flex flex-wrap gap-3">
        <li
          v-for="name in item.images"
          :key="name"
          class="w-40 rounded-lg border border-slate-200 p-2"
          :data-test="`notification-detail-image-${name}`"
        >
          <img
            :src="notificationImageUrl(item.id, name)"
            :alt="name"
            class="h-24 w-full object-contain"
          />
          <div class="magaz-mono mt-1 truncate text-[11px]" :title="name">{{ name }}</div>
          <Button
            label="Удалить"
            size="small"
            text
            severity="danger"
            :data-test="`notification-detail-image-delete-${name}`"
            @click="pendingImage = name"
          />
        </li>
      </ul>
    </section>

    <Message
      v-if="notifications.errorMessage"
      severity="error"
      :closable="false"
      data-test="notification-detail-error"
    >
      {{ notifications.errorMessage }}
    </Message>

    <section v-if="report" class="grid gap-4 md:grid-cols-4">
      <div class="rounded-xl border border-slate-200 bg-white p-4">
        <div class="magaz-muted text-xs">Ознакомлены</div>
        <div class="text-2xl font-semibold" data-test="notification-detail-progress">
          {{ report.totals.acknowledged }} из {{ report.totals.receipts }}
        </div>
        <div class="magaz-muted text-xs">{{ report.totals.percent }}% сессий</div>
      </div>
      <div class="rounded-xl border border-slate-200 bg-white p-4">
        <div class="magaz-muted text-xs">Отложено (не ознакомлено)</div>
        <div class="text-2xl font-semibold" data-test="notification-detail-snoozed">
          {{ report.totals.snoozed }}
        </div>
        <div class="magaz-muted text-xs">вернётся через {{ report.snooze_min }} мин (П-2)</div>
      </div>
      <div class="rounded-xl border border-slate-200 bg-white p-4">
        <div class="magaz-muted text-xs">Магазины</div>
        <div class="text-2xl font-semibold">
          {{ report.totals.stores_delivered }} из {{ report.totals.stores }}
        </div>
        <div class="magaz-muted text-xs">доставлено агенту, а не «показано людям»</div>
      </div>
      <div class="rounded-xl border border-slate-200 bg-white p-4">
        <div class="magaz-muted text-xs">Дедлайн ознакомления</div>
        <div class="text-sm font-semibold" data-test="notification-detail-deadline">
          {{ formatTime(report.deadline_at) }}
        </div>
        <div class="magaz-muted text-xs">после дедлайна строка становится «просрочено»</div>
      </div>
    </section>

    <section
      v-for="store in report?.stores ?? []"
      :key="store.store_id"
      class="overflow-hidden rounded-xl border border-slate-200 bg-white"
      :data-test="`notification-store-${store.store_id}`"
    >
      <div class="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 px-4 py-3">
        <div>
          <RouterLink
            :to="{ name: 'store-detail', params: { id: store.store_id } }"
            class="font-medium text-sky-700 hover:underline"
          >
            {{ store.store_name ?? `Магазин id=${store.store_id}` }}
          </RouterLink>
          <span v-if="!store.store_is_active" class="magaz-muted text-xs"> (отключён)</span>
          <div class="magaz-muted text-xs">
            доставлено {{ formatTime(store.delivered_at) }} · сессий на кассе
            {{ store.sessions_count }}
          </div>
        </div>
        <div class="text-sm" :data-test="`notification-store-progress-${store.store_id}`">
          ознакомлены {{ store.acknowledged }} из {{ store.total }}
        </div>
      </div>

      <table class="magaz-table">
        <thead>
          <tr>
            <th>Пользователь (Windows)</th>
            <th class="w-24">Session ID</th>
            <th class="w-32">Статус</th>
            <th class="w-44">Показано</th>
            <th class="w-44">Ознакомлен</th>
            <th class="w-44">Отложено</th>
            <th>Комментарий</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in sessionRows(store)"
            :key="`${store.store_id}-${row.session_id}`"
            :data-test="`notification-session-${store.store_id}-${row.session_id}`"
          >
            <td>{{ row.session_user }}</td>
            <td class="magaz-muted">{{ row.session_id }}</td>
            <td>
              <Tag
                :value="receiptLabel(row.status)"
                :severity="receiptTone(row.status)"
                :data-test="`notification-session-status-${store.store_id}-${row.session_id}`"
              />
              <div v-if="row.is_overdue" class="magaz-muted text-xs">просрочено</div>
            </td>
            <td class="magaz-muted">{{ formatTime(row.shown_at) }}</td>
            <td class="magaz-muted">{{ formatTime(row.acknowledged_at) }}</td>
            <td class="magaz-muted">
              <span v-if="row.snooze_count">
                {{ row.snooze_count }} раз до {{ formatTime(row.snooze_until) }}
              </span>
              <span v-else>—</span>
            </td>
            <td class="magaz-muted">{{ row.ack_comment ?? '—' }}</td>
          </tr>
          <tr v-if="!sessionRows(store).length">
            <td colspan="7" class="magaz-muted py-4 text-center">
              Уведомление доставлено, но на кассе никто не вошёл под своей учётной записью —
              сессий для отчёта нет (§4.8).
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <p class="magaz-muted text-xs">
      Статусы сессий: {{ Object.values(RECEIPT_LABELS).join(' · ') }}. «Отложено» не считается
      ознакомлением: окно вернётся к пользователю через {{ report?.snooze_min ?? 15 }} мин (§4.9, П-2).
    </p>
    <ConfirmDeleteDialog
      :visible="pendingDelete"
      header="Удалить уведомление?"
      :message="`«${item?.title ?? ''}» исчезнет вместе с отчётом «кто ознакомился».`"
      :details="[
        'Отметки «ознакомлен», «отложено» и адресаты магазинов будут удалены',
        'Картинки тела удаляются с сервера вместе с файлами',
        'Незавершённые задачи рассылки закроются: офлайн-кассы его не получат',
        'Окна, уже показанные сотрудникам, отозвать нельзя — как и при отмене (§7.4)',
      ]"
      :loading="isDeleting"
      @update:visible="(value: boolean) => (pendingDelete = value)"
      @confirm="confirmDelete"
    />

    <ConfirmDeleteDialog
      :visible="pendingImage !== null"
      header="Удалить картинку из уведомления?"
      :message="`Картинка «${pendingImage ?? ''}» будет удалена с сервера, а её метка — из тела уведомления.`"
      :details="[
        'Кассы, уже получившие уведомление, сохранят картинку в своём кэше (§7.4)',
        'Повторно приложить её можно только созданием или правкой уведомления',
      ]"
      :loading="isImageBusy"
      @update:visible="(value: boolean) => { if (!value) pendingImage = null }"
      @confirm="confirmImageDelete"
    />
  </div>
</template>
