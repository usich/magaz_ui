"""Подменённый агент для тестов сервера (Э2, §13: «все внешние вызовы — моками»).

Здесь живут два инструмента:

* `health_body()` — тело ответа `GET /health` агента (§5.1.2);
* `*_transport()` — `httpx.MockTransport` с нужным поведением: успех, таймаут,
  статус, счётчик параллельных запросов.

Благодаря этому проверки мониторинга и кнопки «Проверить сейчас» не зависят ни
от кассы, ни от WireGuard-туннеля.
"""

from __future__ import annotations

import asyncio
import httpx

__all__ = [
    "ConcurrencyProbe",
    "health_body",
    "ok_transport",
    "status_transport",
    "timeout_transport",
    "transport_from_handler",
]


def health_body(**overrides: object) -> dict[str, object]:
    """Тело `GET /health` агента (§5.1.2) с возможностью точечной подмены полей."""
    body: dict[str, object] = {
        "status": "online",
        "store_id": 1,
        "agent_version": "1.0.0",
        "uptime_sec": 3600,
        "work_dir": r"C:\MagazAgent",
        "base_1c_path": "C:/Program Files (x86)/1cv8",
        "os": "Windows 10 Pro 22H2",
        "busy": {"merge": False, "update": False},
        "sessions": [{"user": "user1", "sid": "1"}],
        "credentials_version": 1,
        "devices_version": 1,
    }
    body.update(overrides)
    return body


def transport_from_handler(handler) -> httpx.MockTransport:  # noqa: ANN001 - callable
    """Собрать `MockTransport` из функции-обработчика (request → response)."""
    return httpx.MockTransport(handler)


def ok_transport(body: dict[str, object] | None = None) -> httpx.MockTransport:
    """Агент отвечает `200` с телом `/health` (по умолчанию — «всё хорошо»)."""
    payload = body if body is not None else health_body()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json={"agent_version": payload.get("agent_version")})
        return httpx.Response(200, json=payload)

    return transport_from_handler(handler)


def timeout_transport() -> httpx.MockTransport:
    """Агент недоступен: соединение не устанавливается (таймаут, §3.3)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("агент не отвечает", request=request)

    return transport_from_handler(handler)


def status_transport(status_code: int, body: dict[str, object] | None = None) -> httpx.MockTransport:
    """Агент отвечает заданным статусом (например, `401` после ротации токена)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body or {})

    return transport_from_handler(handler)


class ConcurrencyProbe:
    """Транспорт-счётчик: сколько запросов к агентам выполнялось одновременно.

    Используется для проверки семафора `health_concurrency` (§3.2): ответ
    задерживается, поэтому «в полёте» оказывается ровно разрешённое число.
    """

    def __init__(self, *, delay_sec: float = 0.02, **overrides: object) -> None:
        self.delay_sec = delay_sec
        self.body = health_body(**overrides)
        self.active = 0
        self.max_active = 0
        self.calls = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.active += 1
        self.calls += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay_sec)
            return httpx.Response(200, json=self.body)
        finally:
            self.active -= 1

    @property
    def transport(self) -> httpx.MockTransport:
        """Транспорт, который можно передать в клиент/приложение."""
        return httpx.MockTransport(self.handle)
