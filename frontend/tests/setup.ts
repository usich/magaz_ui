/**
 * Общая подготовка тестов Vitest (§13.2).
 *
 * Тесты бьют по SPA изолированно: сеть подменяется моками, реальный сервер
 * дашборда не нужен. Здесь глушим предупреждения jsdom, которые не относятся
 * к делу, и добавляем отсутствующие в jsdom браузерные API.
 */

import { afterEach, vi } from 'vitest'
import { registerUnauthorizedHandler } from '@/api/client'

// jsdom не умеет matchMedia — PrimeVue обращается к нему при инициализации темы
if (!window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }),
  })
}

afterEach(() => {
  registerUnauthorizedHandler(null)
  vi.restoreAllMocks()
})
