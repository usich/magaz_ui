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
import json

import httpx

__all__ = [
    "ConcurrencyProbe",
    "ScriptedAgent",
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


class ScriptedAgent:
    """Мок агента для «эталонного сценария» Э4 (§13: внешние вызовы — моками).

    Агент ведёт себя как настоящий: принимает `/run-*` и отвечает `202`, затем на
    `/check-*` отвечает `202`, пока «операция идёт», и наконец `200` с логом (или
    `400` с ошибкой). Переходы настраиваются, поэтому один и тот же мок годится
    для успеха, ошибки, таймаута и «1С не запустилась».

    Все запросы записываются в `requests`, а тела — в `json_bodies()`: по ним
    проверяется, что пароль 1С в командах не появляется (I-7).
    """

    def __init__(
        self,
        *,
        checks_before_success: int | None = 1,
        fail_operation: bool = False,
        busy: bool = True,
        interrupted: str | None = None,
        cf_exists: bool = False,
        cfe_exists: bool = False,
        xml_exists: bool = False,
        download_polls_before_file: int = 1,
        credentials_version: int = 1,
        devices_version: int = 1,
        device_status: str = "online",
        device_latency_ms: int = 12,
        merge_message: str = "Merge process started in the background.",
        update_message: str = "Update DB process started in the background.",
    ) -> None:
        self.checks_before_success = checks_before_success
        self.fail_operation = fail_operation
        self.busy = busy
        self.interrupted = interrupted
        self.cf_exists = cf_exists
        self.cfe_exists = cfe_exists
        self.xml_exists = xml_exists
        self.download_polls_before_file = download_polls_before_file
        self.download_polls = 0
        self.download_requested = False
        self.credentials_version = credentials_version
        self.devices_version = devices_version
        self.merge_message = merge_message
        self.update_message = update_message

        self.requests: list[dict[str, object]] = []
        self.checks = 0
        self.operation: str | None = None
        self.credentials_pushes: list[dict[str, object]] = []
        #: Д-1: принятые push-и списка оборудования и внеочередные проверки
        self.devices_pushes: list[dict[str, object]] = []
        self.devices_checks: list[list[dict[str, object]]] = []
        self.device_status = device_status
        self.device_latency_ms = device_latency_ms
        #: Э5: принятые push-и уведомлений (ФН3) и число «видимых» сессий
        self.notify_pushes: list[dict[str, object]] = []
        self.sessions_online = 2

    # --- Диагностика для тестов --------------------------------------------

    def paths(self) -> list[str]:
        """Только пути запросов — удобно проверять «что и в каком порядке звали»."""
        return [str(item["path"]) for item in self.requests]

    def json_bodies(self) -> list[object]:
        """Тела всех запросов (для проверок «пароля нигде нет», I-7)."""
        return [item["json"] for item in self.requests if item["json"] is not None]

    @property
    def transport(self) -> httpx.MockTransport:
        """Транспорт для клиентов сервера (`AgentClient(transport=...)`)."""
        return httpx.MockTransport(self.handle)

    # --- Обработчик ---------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Ответить так, как это сделала бы касса (§5.1.1, §5.1.2)."""
        path = request.url.path
        try:
            body = json.loads(request.content.decode("utf-8")) if request.content else None
        except ValueError:
            body = None
        self.requests.append(
            {
                "method": request.method,
                "path": path,
                "json": body,
                "query": dict(request.url.params),
            }
        )

        if path == "/health":
            return httpx.Response(200, json=self._health())
        if path == "/version":
            return httpx.Response(200, json={"agent_version": "1.0.0", "store_id": 1})
        if path == "/credentials":
            self.credentials_pushes.append(body or {})
            self.credentials_version = int((body or {}).get("credentials_version") or 1)
            return httpx.Response(
                200, json={"stored": True, "credentials_version": self.credentials_version}
            )
        if path == "/devices/config":
            self.devices_pushes.append(body or {})
            self.devices_version = int((body or {}).get("devices_version") or 1)
            return httpx.Response(
                200, json={"stored": True, "devices_version": self.devices_version}
            )
        if path == "/devices/check-now":
            devices = list((body or {}).get("devices") or [])
            self.devices_checks.append(devices)
            online = self.device_status == "online"
            return httpx.Response(
                200,
                json=[
                    {
                        "ip": item.get("ip"),
                        "status": self.device_status,
                        "latency_ms": self.device_latency_ms if online else None,
                        "error": None if online else "нет ответа",
                    }
                    for item in devices
                ],
            )
        if path == "/notify":
            self.notify_pushes.append(body or {})
            return httpx.Response(
                200, json={"queued": True, "sessions": int(self.sessions_online)}
            )
        if path == "/check-files":
            if self.download_requested:
                self.download_polls += 1
            return httpx.Response(200, json=self._files())
        if path == "/download-file":
            self.download_polls = 0
            self.download_requested = True
            return httpx.Response(202, json={"message": "Download started in the background."})
        if path == "/send-message":
            return httpx.Response(
                200,
                json={
                    "message": "Message sent and 1cv8 processes terminated.",
                    "killed_1cv8": True,
                },
            )
        if path in ("/run-merge", "/run-update-db"):
            self.checks = 0
            self.operation = path
            message = self.merge_message if path == "/run-merge" else self.update_message
            return httpx.Response(
                202, json={"message": message, "log_path": self._log_path(path)}
            )
        if path in ("/check-merge", "/check-update-db"):
            return self._check_state(path)
        return httpx.Response(404, json={"detail": f"Неизвестный путь {path}"})

    # --- Тела ответов -------------------------------------------------------

    def _health(self) -> dict[str, object]:
        """`GET /health` (§5.1.2) — с `busy`, версиями П-1/Д-1 и прерванной операцией."""
        return health_body(
            busy={"merge": self.busy, "update": self.busy},
            credentials_version=self.credentials_version,
            devices_version=self.devices_version,
            interrupted_operation=self.interrupted,
        )

    def _files(self) -> dict[str, object]:
        """`POST /check-files`: файл «появляется» после скачивания (ФН2, §11.3)."""
        downloaded = self.download_requested and self.download_polls >= self.download_polls_before_file
        has_cf = bool(self.cf_exists or downloaded)
        return {
            "cf_exists": has_cf,
            "cfe_exists": bool(self.cfe_exists),
            "xml_exists": bool(self.xml_exists),
            "cf_size": 1024 if has_cf else None,
            "cfe_size": None,
            "xml_size": None,
            "cf_mtime": "2026-10-13T10:00:00Z" if has_cf else None,
            "cfe_mtime": None,
            "xml_mtime": None,
        }

    def _log_path(self, path: str) -> str:
        """Путь лога на кассе — имена как в v2 (§7.1, Приложение B)."""
        if path == "/run-merge":
            return r"C:\MagazAgent\log-merge.txt"
        return r"C:\MagazAgent\log-update-db.txt"

    def _check_state(self, path: str) -> httpx.Response:
        """`GET /check-merge` / `GET /check-update-db`: `202 → 200/400` (§3.3)."""
        if self.checks_before_success is None:
            return httpx.Response(
                202,
                json={
                    "status": "in_progress",
                    "message": "Operation is in progress.",
                    "logs": "",
                },
            )
        self.checks += 1
        if self.checks < self.checks_before_success:
            return httpx.Response(
                202,
                json={
                    "status": "in_progress",
                    "message": "Operation is in progress.",
                    "logs": "",
                },
            )
        if self.fail_operation:
            return httpx.Response(
                400,
                json={
                    "status": "failed",
                    "message": "Merge failed.",
                    "logs": "Ошибка: конфликт объектов метаданных",
                },
            )
        success = (
            "Объединение конфигураций успешно завершено"
            if path == "/check-merge"
            else "Обновление конфигурации успешно завершено"
        )
        return httpx.Response(
            200,
            json={
                "status": "success",
                "message": "Completed successfully.",
                "logs": f"{success}\nДлительность: 12 с",
            },
        )
