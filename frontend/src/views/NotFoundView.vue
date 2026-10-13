<script setup lang="ts">
/** Страница 404: показывается вне оболочки, чтобы не пугать новым меню. */
import Button from 'primevue/button'
</script>

<template>
  <div class="flex min-h-screen flex-col items-center justify-center gap-4 bg-slate-100 text-center">
    <p class="text-6xl font-bold text-slate-300">404</p>
    <h1 class="text-xl font-semibold">Такой страницы нет</h1>
    <p class="magaz-muted max-w-md text-sm">
      Проверьте адрес. Разделы дашборда перечислены в меню слева.
    </p>
    <Button label="На главную" icon="pi pi-home" @click="$router.push({ name: 'dashboard' })" />
  </div>
</template>
