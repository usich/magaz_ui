"""Опрос оборудования подсети на кассе (Д-1, Э4Б, §7.6).

Проверки идут без реальной сети и без `ping.exe`: внешние процессы подменены
провайдером (§7.5), TCP-connect — функцией `asyncio.open_connection`, сервер —
`httpx.MockTransport`. Проверяется то, что важно на кассе:

* команда ping собирается списком argv (shell не участвует, I-15);
* TCP-проверка нужна камерам, которые молчат на ICMP;
* результат батчем уходит на сервер, а при недоступном сервере не теряется —
  лежит в буфере и досылается следующим циклом;
* список устройств приходит push-ом и остаётся копией в `devices.json`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from client.agent import devices
from client.agent.app import create_agent_app
from client.agent.config import AGENT_DEVICES_FILE_NAME
from shared.enums import CheckType, DeviceStatus
from shared.schemas import DeviceCheckSpec, DeviceSpec, DevicesConfigPush
from tests.client.conftest import make_config

#: Токен магазина — как в `agent-config.json` (§7.2)
HEADERS = {"X-Agent-Token": "agent-token-for-tests"}

PING_CODE_OK = 0
PING_CODE_NO_ANSWER = 1


class StubProvider:
    """Провайдер процессов с настраиваемым кодом возврата (§7.5).

    Реальный `ping.exe` не запускается: тест проверяет только то, **какую**
    команду собрал агент и как он разобрал код возврата.
    """

    def __init__(
        self,
        *,
        code: int | None = PING_CODE_OK,
        missing: bool = False,
        delay_sec: float = 0.0,
    ) -> None:
        self.code = code
        self.missing = missing
        self.delay_sec = delay_sec
        self.commands: list[list[str]] = []
        self.active = 0
        self.max_active = 0

    async def run_captured(  # noqa: ANN001 - интерфейс провайдера
        self, args: list[str], *, log_path: Path | None = None, timeout: float | None = None
    ) -> int | None:
        """Запомнить argv и вернуть заданный код (как это сделал бы `ping.exe`)."""
        self.commands.append(list(args))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay_sec:
                await asyncio.sleep(self.delay_sec)
            if self.missing:
                raise FileNotFoundError("ping.exe")
            return self.code
        finally:
            self.active -= 1

    async def run_quiet(self, args: list[str]) -> bool:
        """`msg`/`taskkill` здесь не нужны — считаем утилиты доступными."""
        return True


class TestDeviceChecker:
    """§7.6: `icmp` — `ping.exe` argv, `tcp` — TCP-connect."""

    async def test_icmp_uses_argv_without_shell(self) -> None:
        provider = StubProvider(code=PING_CODE_OK)
        checker = devices.DeviceChecker(provider=provider, timeout_ms=750)

        result = await checker.check(
            DeviceSpec(name="камера вход", ip="10.10.1.21", check_type=CheckType.ICMP)
        )

        assert result.status == DeviceStatus.ONLINE
        assert result.latency_ms is not None and result.latency_ms >= 0
        assert provider.commands == [["ping.exe", "-n", "1", "-w", "750", "10.10.1.21"]], (
            "I-15: команда собирается списком argv, shell не участвует"
        )

    async def test_icmp_without_answer_is_offline(self) -> None:
        checker = devices.DeviceChecker(provider=StubProvider(code=PING_CODE_NO_ANSWER))

        result = await checker.check(DeviceSpec(name="камера склад", ip="10.10.1.22"))

        assert result.status == DeviceStatus.OFFLINE
        assert devices.PING_MISS_ERROR in (result.error or "")

    async def test_missing_ping_is_offline_with_reason(self) -> None:
        checker = devices.DeviceChecker(provider=StubProvider(missing=True))

        result = await checker.check(DeviceSpec(name="свитч", ip="10.10.1.30"))

        assert result.status == DeviceStatus.OFFLINE
        assert result.error == "ping.exe недоступен"

    async def test_tcp_connect_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Камера, молчащая на ICMP, проверяется соединением на порт (Д-1)."""
        calls: list[tuple[str, int]] = []

        class FakeWriter:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                return None

        async def fake_open_connection(host: str, port: int):  # noqa: ANN202 - заглушка
            calls.append((host, port))
            return object(), FakeWriter()

        monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
        checker = devices.DeviceChecker(provider=StubProvider(), timeout_ms=500)

        result = await checker.check(
            DeviceSpec(name="камера двор", ip="10.10.1.40", check_type=CheckType.TCP, port=554)
        )

        assert result.status == DeviceStatus.ONLINE
        assert calls == [("10.10.1.40", 554)]

    async def test_tcp_refused_is_offline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def refused(host: str, port: int):  # noqa: ANN202 - заглушка
            raise ConnectionRefusedError("отказано")

        monkeypatch.setattr(asyncio, "open_connection", refused)
        checker = devices.DeviceChecker(provider=StubProvider())

        result = await checker.check(
            DeviceSpec(name="принтер", ip="10.10.1.50", check_type=CheckType.TCP, port=9100)
        )

        assert result.status == DeviceStatus.OFFLINE
        assert "9100" in (result.error or "")

    async def test_check_many_limits_parallelism(self) -> None:
        """§7.6: одновременно не больше `concurrency` проверок — касса не «сервер»."""
        provider = StubProvider(delay_sec=0.01)
        checker = devices.DeviceChecker(provider=provider, concurrency=2)
        specs = [
            DeviceSpec(name=f"камера {index}", ip=f"10.10.1.{index + 1}") for index in range(6)
        ]

        results = await checker.check_many(specs)

        assert len(results) == 6
        assert provider.max_active <= 2



class TestDevicesStore:
    """§7.6: копия списка на диске — опрос продолжается без сервера."""

    def test_apply_push_saves_snapshot(self, tmp_path: Path) -> None:
        path = tmp_path / AGENT_DEVICES_FILE_NAME
        store = devices.DevicesStore.load(path)

        stored = store.apply_push(
            DevicesConfigPush(
                devices_version=4,
                check_interval_sec=30,
                ping_timeout_ms=500,
                devices=[
                    DeviceSpec(name="камера вход", ip="10.10.1.21"),
                    DeviceSpec(
                        name="камера двор",
                        ip="10.10.1.40",
                        check_type=CheckType.TCP,
                        port=554,
                    ),
                    DeviceSpec(name="отключённая", ip="10.10.1.99", is_active=False),
                ],
            )
        )

        assert stored.devices_version == 4
        assert stored.check_interval_sec == 30
        assert stored.ping_timeout_ms == 500
        assert [item.ip for item in store.active()] == ["10.10.1.21", "10.10.1.40"]

        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["devices_version"] == 4
        assert saved["check_interval_sec"] == 30
        assert len(saved["devices"]) == 3

    def test_duplicate_ip_is_collapsed(self, tmp_path: Path) -> None:
        """Пара «магазин + IP» уникальна (§4.11): в копии тоже одна запись."""
        store = devices.DevicesStore.load(tmp_path / AGENT_DEVICES_FILE_NAME)

        stored = store.apply_push(
            DevicesConfigPush(
                devices_version=2,
                devices=[
                    DeviceSpec(name="камера", ip="10.10.1.21"),
                    DeviceSpec(name="камера (переименована)", ip="10.10.1.21"),
                ],
            )
        )

        assert [item.name for item in stored.devices] == ["камера (переименована)"]

    def test_missing_snapshot_keeps_config_schedule(self, tmp_path: Path) -> None:
        store = devices.DevicesStore.load(
            tmp_path / AGENT_DEVICES_FILE_NAME, interval_sec=90, timeout_ms=2000
        )

        assert store.config.devices == []
        assert store.config.check_interval_sec == 90
        assert store.config.ping_timeout_ms == 2000
        assert store.has_snapshot is False

    def test_load_restores_snapshot_after_restart(self, tmp_path: Path) -> None:
        path = tmp_path / AGENT_DEVICES_FILE_NAME
        devices.DevicesStore.load(path).apply_push(
            DevicesConfigPush(devices_version=7, devices=[DeviceSpec(name="свитч", ip="10.10.1.30")])
        )

        restored = devices.DevicesStore.load(path)

        assert restored.version == 7
        assert restored.has_snapshot is True
        assert [item.ip for item in restored.active()] == ["10.10.1.30"]

    def test_broken_snapshot_does_not_break_polling(self, tmp_path: Path) -> None:
        """Битый `devices.json` — не повод не опрашивать: список пришлют заново."""
        path = tmp_path / AGENT_DEVICES_FILE_NAME
        path.write_text("{не json", encoding="utf-8")

        store = devices.DevicesStore.load(path, interval_sec=60, timeout_ms=1000)

        assert store.config.devices == []
        assert store.config.check_interval_sec == 60



class ScriptedServer:
    """Приём батчей на сервере: запоминает тела и умеет «падать» (§7.6)."""

    def __init__(self, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.batches: list[dict] = []
        self.paths: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Разобрать тело батча — так же, как это сделает сервер (§5.2.2)."""
        self.paths.append(request.url.path)
        self.batches.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(self.status_code, json={"ok": self.status_code < 400})

    @property
    def transport(self) -> httpx.MockTransport:
        """Транспорт для `DeviceStatusSender(transport=...)`."""
        return httpx.MockTransport(self.handler)


def build_monitor(
    tmp_path: Path, server: ScriptedServer, **config_overrides
) -> tuple[devices.DeviceMonitor, devices.DevicesStore]:
    """Монитор с подменённым `ping.exe` и сервером-заглушкой (без сети)."""
    config = make_config(work_dir=str(tmp_path), **config_overrides)
    store = devices.DevicesStore.load(tmp_path / AGENT_DEVICES_FILE_NAME)
    monitor = devices.DeviceMonitor(
        store,
        checker=devices.DeviceChecker(provider=StubProvider(), timeout_ms=1000),
        sender=devices.DeviceStatusSender(config, transport=server.transport),
        store_id=config.store_id,
        startup_delay_sec=0.0,
    )
    return monitor, store


class TestDeviceMonitor:
    """§7.6: расписание опроса и батч результатов на сервер."""

    async def test_poll_once_sends_batch_for_active_devices(self, tmp_path: Path) -> None:
        server = ScriptedServer()
        monitor, store = build_monitor(tmp_path, server)
        store.apply_push(
            DevicesConfigPush(
                devices_version=3,
                devices=[
                    DeviceSpec(name="камера вход", ip="10.10.1.21"),
                    DeviceSpec(name="отключённая камера", ip="10.10.1.22", is_active=False),
                ],
            )
        )

        results = await monitor.poll_once()

        assert [item.ip for item in results] == ["10.10.1.21"], "отключённые не опрашиваются"
        assert server.paths == [devices.DEVICES_STATUS_PATH]
        batch = server.batches[0]
        assert batch["store_id"] == 12
        assert batch["agent_ok"] is True
        assert batch["devices"][0]["ip"] == "10.10.1.21"
        assert batch["devices"][0]["status"] == "online"
        assert monitor.pending == 0

    async def test_offline_server_keeps_batch_in_buffer(self, tmp_path: Path) -> None:
        server = ScriptedServer(status_code=503)
        monitor, store = build_monitor(tmp_path, server)
        store.apply_push(
            DevicesConfigPush(devices_version=1, devices=[DeviceSpec(name="свитч", ip="10.10.1.30")])
        )

        await monitor.poll_once()
        assert monitor.pending == 1, "сервер не принял — батч не потерян (§7.6)"

        server.status_code = 200
        sent = await monitor.flush()

        assert sent == 1
        assert monitor.pending == 0
        assert server.batches[-1]["devices"][0]["ip"] == "10.10.1.30"

    async def test_interval_comes_from_pushed_schedule(self, tmp_path: Path) -> None:
        """Д-9: расписание опроса берётся из настроек сервера, а не из кода."""
        server = ScriptedServer()
        monitor, store = build_monitor(tmp_path, server)

        store.apply_push(DevicesConfigPush(devices_version=2, check_interval_sec=15))

        assert monitor.interval_sec == 15.0

    async def test_check_now_does_not_buffer_results(self, tmp_path: Path) -> None:
        """Ответ «Проверить сейчас» синхронный: запись результата делает сервер."""
        server = ScriptedServer()
        monitor, _ = build_monitor(tmp_path, server)

        results = await monitor.check_now([DeviceCheckSpec(ip="10.10.1.21")])

        assert [item.status for item in results] == [DeviceStatus.ONLINE]
        assert monitor.pending == 0
        assert server.batches == []



def build_agent(tmp_path: Path, *, devices_monitor: bool | None = False, **overrides):  # noqa: ANN201
    """Приложение агента с временной рабочей папкой и подменённым ping (§7.5).

    `devices_monitor=False` — умолчание тестов: фоновое расписание поднимается
    только там, где это проверяется, и никогда не ходит в сеть.
    """
    config = make_config(work_dir=str(tmp_path), **overrides)
    provider = StubProvider()
    app = create_agent_app(
        config,
        config_path=config.work_path / "agent-config.json",
        provider=provider,
        devices_monitor=devices_monitor,
    )
    return app, config, provider


class TestAgentDevicesApi:
    """§5.1.2: `/devices/config` (Д-1) и `/devices/check-now` (кнопка в дашборде)."""

    def test_endpoints_require_agent_token(self, tmp_path: Path) -> None:
        app, _, _ = build_agent(tmp_path)

        with TestClient(app) as client:
            assert client.post("/devices/config", json={"devices_version": 2}).status_code == 401
            assert (
                client.post("/devices/check-now", json={"devices": [{"ip": "10.10.1.21"}]}).status_code
                == 401
            )

    def test_push_config_saves_snapshot_and_reports_version(self, tmp_path: Path) -> None:
        app, config, _ = build_agent(tmp_path)

        with TestClient(app) as client:
            response = client.post(
                "/devices/config",
                json={
                    "devices_version": 7,
                    "check_interval_sec": 30,
                    "ping_timeout_ms": 400,
                    "devices": [{"name": "камера вход", "ip": "10.10.1.21"}],
                },
                headers=HEADERS,
            )

            assert response.status_code == 200
            assert response.json() == {
                "stored": True,
                "devices_version": 7,
                "devices": 1,
                "active": 1,
                "check_interval_sec": 30,
            }
            assert config.devices_version == 7, "версия сразу видна в /health (§7.6)"
            health = client.get("/health", headers=HEADERS).json()
            assert health["devices_version"] == 7

        snapshot = json.loads((tmp_path / AGENT_DEVICES_FILE_NAME).read_text(encoding="utf-8"))
        assert snapshot["devices_version"] == 7
        assert snapshot["devices"][0]["ip"] == "10.10.1.21"

    def test_check_now_returns_results_of_ping(self, tmp_path: Path) -> None:
        app, _, provider = build_agent(tmp_path)

        with TestClient(app) as client:
            response = client.post(
                "/devices/check-now",
                json={"devices": [{"ip": "10.10.1.21"}, {"ip": "10.10.1.22"}]},
                headers=HEADERS,
            )

        assert response.status_code == 200
        results = response.json()
        assert [item["ip"] for item in results] == ["10.10.1.21", "10.10.1.22"]
        assert {item["status"] for item in results} == {"online"}
        assert provider.commands == [
            ["ping.exe", "-n", "1", "-w", "1000", "10.10.1.21"],
            ["ping.exe", "-n", "1", "-w", "1000", "10.10.1.22"],
        ]

    def test_check_now_requires_port_for_tcp(self, tmp_path: Path) -> None:
        """§5.1.2: TCP-проверка без порта — ошибка контракта (`422`), а не 500."""
        app, _, _ = build_agent(tmp_path)

        with TestClient(app) as client:
            response = client.post(
                "/devices/check-now",
                json={"devices": [{"ip": "10.10.1.40", "check_type": "tcp"}]},
                headers=HEADERS,
            )

        assert response.status_code == 422


class TestDevicesMonitorLifecycle:
    """§7.6: расписание поднимается только когда есть куда докладывать."""

    def test_monitor_starts_and_stops_with_app(self, tmp_path: Path) -> None:
        app, _, _ = build_agent(
            tmp_path, devices_monitor=True, server_url="http://127.0.0.1:9"
        )

        with TestClient(app):
            assert app.state.agent_devices.is_running is True

        assert app.state.agent_devices.is_running is False

    def test_monitor_is_off_without_server_url(self, tmp_path: Path) -> None:
        app, _, _ = build_agent(tmp_path, devices_monitor=None, server_url="")

        with TestClient(app):
            assert app.state.agent_devices.is_running is False

