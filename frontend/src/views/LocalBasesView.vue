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
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'

import { ApiError } from '@/api/client'
import type { LocalBaseItem } from '@/api/types'
import LocalBaseFormDialog from '@/components/LocalBaseFormDialog.vue'
import { useLocalBasesStore } from '@/stores/local_bases'

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

onMounted(() => void store.load())

function openCreate(): void {
  editingBase.value = null
  formVisible.value = true
}
function openEdit(base: LocalBaseItem): void {
  editingBase.value = base
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
      <Button
        label="Добавить базу"
        icon="pi pi-plus"
        size="small"
        data-test="lb-add"
        @click="openCreate"
      />
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
              <th class="px-4 py-2 font-medium">Название</th>
              <th class="py-2 pr-3 font-medium">Версия 1С</th>
              <th class="py-2 pr-3 font-medium">Кластер / БД</th>
              <th class="py-2 pr-3 font-medium">Пользователь</th>
              <th class="py-2 pr-3 font-medium">Путь к базе</th>
              <th class="py-2 pr-3 font-medium">Бэкап</th>
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
              <td class="py-2 pr-3 text-right whitespace-nowrap">
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
              <td colspan="7" class="magaz-muted py-4 text-center">Нет баз в этой группе</td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>

    <div v-else class="magaz-muted rounded-xl border border-dashed border-slate-300 p-6 text-sm">
      Локальные базы ещё не добавлены — нажмите «Добавить базу».
    </div>

    <LocalBaseFormDialog v-model:visible="formVisible" :base="editingBase" @saved="onSaved" />
  </div>
</template>

