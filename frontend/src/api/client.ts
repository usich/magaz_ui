/**
 * Единый HTTP-клиент админского API `/api/v1/admin/*` (§6: `src/api`).
 *
 * Правила (одно место на весь SPA):
 *  - cookie сессии уходит автоматически (`credentials: 'include'`);
 *  - `401` → вызывается обработчик, который уводит на `/login`;
 *  - `409` (конфликт операции) и `422` (невалидное поле) приходят как `ApiError`
 *    с исходным `detail` — представления показывают текст, а не «Ошибка 500»;
 *  - сетевой сбой → `ApiError` со статусом 0 («сервер недоступен»).
 */

export interface ValidationDetail {
  loc?: (string | number)[]
  msg: string
  type?: string
}

export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(message: string, status: number, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }

  get isUnauthorized(): boolean {
    return this.status === 401
  }

  get isConflict(): boolean {
    return this.status === 409
  }

  get isValidation(): boolean {
    return this.status === 422
  }

  /** Плоский список сообщений: строки из `detail` FastAPI. */
  get messages(): string[] {
    const detail = this.detail as { detail?: unknown } | string[] | unknown
    const raw =
      detail && typeof detail === 'object' && 'detail' in (detail as Record<string, unknown>)
        ? (detail as { detail: unknown }).detail
        : detail

    if (typeof raw === 'string') return [raw]
    if (Array.isArray(raw)) {
      return raw.map((item) => {
        if (typeof item === 'string') return item
        const entry = item as ValidationDetail
        return entry?.msg ?? JSON.stringify(item)
      })
    }
    return []
  }
}

type UnauthorizedHandler = () => void

let unauthorizedHandler: UnauthorizedHandler | null = null

/** Обработчик 401 — ставится в `main.ts` (уводит на страницу входа). */
export function registerUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: unknown
  signal?: AbortSignal
  /** Для `GET /auth/me`: 401 — нормальная ситуация «не вошёл», а не сбой сессии. */
  skipUnauthorizedHandler?: boolean
}

export const API_BASE = '/api/v1'

/** Выполнить запрос к API и вернуть разобранный ответ. */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, signal, skipUnauthorizedHandler = false } = options
  const hasBody = body !== undefined

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      credentials: 'include',
      headers: {
        Accept: 'application/json',
        ...(hasBody ? { 'Content-Type': 'application/json' } : {}),
      },
      body: hasBody ? JSON.stringify(body) : undefined,
      signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError('Сервер дашборда недоступен', 0)
  }

  if (response.status === 204) {
    return undefined as T
  }

  const text = await response.text()
  const parsed = text ? safeJsonParse(text) : undefined

  if (!response.ok) {
    if (response.status === 401 && !skipUnauthorizedHandler) {
      unauthorizedHandler?.()
    }
    throw new ApiError(errorMessage(response.status, parsed), response.status, parsed)
  }

  return parsed as T
}

function errorMessage(status: number, parsed: unknown): string {
  const detail = parsed as { detail?: unknown } | undefined
  const raw = detail && typeof detail === 'object' ? detail.detail : undefined

  if (typeof raw === 'string' && raw.trim()) return raw
  if (Array.isArray(raw) && raw.length > 0) {
    const first = raw[0] as ValidationDetail
    if (first?.msg) return first.msg
  }
  if (status === 401) return 'Требуется вход в дашборд'
  if (status === 403) return 'Недостаточно прав'
  if (status === 404) return 'Не найдено'
  if (status === 409) return 'Конфликт: операция уже выполняется или серверная ИБ занята другой операцией (R12)'
  if (status === 422) return 'Проверьте заполненные поля'
  return `Ошибка ${status}`
}

function safeJsonParse(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

export const api = {
  get: <T>(path: string, options: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    request<T>(path, { ...options, method: 'GET' }),
  post: <T>(path: string, body?: unknown, options: Omit<RequestOptions, 'method'> = {}) =>
    request<T>(path, { ...options, method: 'POST', body }),
  put: <T>(path: string, body?: unknown, options: Omit<RequestOptions, 'method'> = {}) =>
    request<T>(path, { ...options, method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown, options: Omit<RequestOptions, 'method'> = {}) =>
    request<T>(path, { ...options, method: 'PATCH', body }),
  delete: <T>(path: string, options: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    request<T>(path, { ...options, method: 'DELETE' }),
}
