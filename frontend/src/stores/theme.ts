/**
 * Тема интерфейса (§11.1, Э8): светлая, тёмная или «как в системе».
 *
 * PrimeVue настроен на класс `.app-dark` (`main.ts`), поэтому переключение —
 * это одна операция над `<html>`: класс есть — тёмная тема. Выбор хранится в
 * `localStorage`, а тема применяется до монтирования SPA, поэтому после
 * перезагрузки страницы светлая «вспышка» не появляется.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

/** Режимы темы: явный светлый/тёмный и следование настройке ОС */
export type ThemeMode = 'light' | 'dark' | 'system'

/** Ключ `localStorage` с выбранным режимом */
export const THEME_STORAGE_KEY = 'magaz-theme'

/** Класс на `<html>`: включает тёмные токены PrimeVue и Tailwind (`style.css`) */
export const DARK_CLASS = 'app-dark'

/** Подписи режимов для переключателя в шапке (`AppShell`) */
export const THEME_MODE_LABELS: Record<ThemeMode, string> = {
  light: 'Светлая тема',
  dark: 'Тёмная тема',
  system: 'Как в системе',
}

/** Системная настройка, за которой следит режим «как в системе» */
export const DARK_MEDIA_QUERY = '(prefers-color-scheme: dark)'

/** Прочитанный выбор из `localStorage`; по умолчанию — настройка ОС */
function readStoredMode(): ThemeMode {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    if (stored === 'light' || stored === 'dark' || stored === 'system') {
      return stored
    }
  } catch {
    // `localStorage` недоступен (приватный режим) — считаем, что выбора нет
  }
  return 'system'
}

export const useThemeStore = defineStore('theme', () => {
  const mode = ref<ThemeMode>(readStoredMode())
  const systemPrefersDark = ref(false)

  const followsSystem = computed(() => mode.value === 'system')
  const isDark = computed(
    () => mode.value === 'dark' || (followsSystem.value && systemPrefersDark.value),
  )

  let media: MediaQueryList | null = null
  let listener: ((event: MediaQueryListEvent) => void) | null = null

  /** Применить режим к `<html>`: класс для PrimeVue/Tailwind и `data-theme` для своих стилей. */
  function apply(): void {
    const root = document.documentElement
    root.classList.toggle(DARK_CLASS, isDark.value)
    root.dataset.theme = isDark.value ? 'dark' : 'light'
  }

  /** Сменить режим: сохранить выбор и сразу применить его. */
  function setMode(next: ThemeMode): void {
    mode.value = next
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next)
    } catch {
      // Выбор просто не переживёт перезагрузку — тему применяем всё равно
    }
    apply()
  }

  /** Переключить светлую ↔ тёмную (для кнопки-переключателя). */
  function toggle(): void {
    setMode(isDark.value ? 'light' : 'dark')
  }

  /**
   * Подписаться на настройку ОС и применить тему.
   *
   * Вызывается при старте приложения (`main.ts`) и при монтировании шапки;
   * повторные вызовы идемпотентны — слушатель создаётся один раз.
   */
  function initialize(): void {
    if (typeof window.matchMedia !== 'function') {
      apply()
      return
    }
    if (!media) {
      media = window.matchMedia(DARK_MEDIA_QUERY)
      listener = (event) => {
        systemPrefersDark.value = event.matches
        apply()
      }
      media.addEventListener('change', listener)
    }
    systemPrefersDark.value = media.matches
    apply()
  }

  /** Отписаться от настройки ОС (тесты и размонтирование оболочки). */
  function dispose(): void {
    if (media && listener) {
      media.removeEventListener('change', listener)
    }
    media = null
    listener = null
  }

  return {
    mode,
    isDark,
    followsSystem,
    systemPrefersDark,
    setMode,
    toggle,
    apply,
    initialize,
    dispose,
  }
})
