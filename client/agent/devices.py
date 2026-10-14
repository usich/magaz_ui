"""Опрос оборудования подсети магазина (Д-1, §7.6).

Агент — единственный, кто физически видит подсеть магазина (касса стоит внутри
неё), поэтому проверки выполняет он, а сервер только принимает результат
батчем. Проверка устройства:

* `icmp` → `ping.exe -n 1 -w <мс> <ip>` **списком argv, без shell** (I-15, §8.1);
* `tcp` → TCP-connect на `check_port` (`asyncio.open_connection`) — для камер,
  которые молчат на ICMP.

Список устройств приходит push-ом `POST /devices/config` и хранится копией в
`C:\\MagazAgent\\devices.json` (§7.6): опрос продолжается, даже когда сервер
недоступен. Сканирование подсети «наугад» (sweep) запрещено — проверяем только
то, что явно заведено в `store_devices` магазина; параллелизм ограничен
семафором, чтобы не мешать кассе.

Результаты уходят батчем `POST /api/v1/agent/devices/status`; недоступный
сервер — не потеря: батч остаётся в буфере и досылается следующим циклом
(так же ведут себя `ack` уведомлений, §7.4). Расписание (`check_interval_sec`,
`ping_timeout_ms`) приходит вместе со списком (Д-9) — значения по умолчанию
берутся из `agent-config.json` (§7.2).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from client.agent.config import AgentConfig
from client.agent.processes import DEFAULT_PROVIDER, ProcessProvider
from shared.enums import CheckType, DeviceStatus
from shared.schemas import (
    DeviceCheckResult,
    DeviceCheckSpec,
    DeviceSpec,
    DeviceStatusBatch,
    DevicesConfigPush,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEVICES_STATUS_PATH",
    "MAX_BUFFERED_BATCHES",
    "CheckableDevice",
    "DeviceChecker",
    "DeviceMonitor",
    "DeviceStatusSender",
    "DevicesConfig",
    "DevicesStore",
    "load_devices",
    "save_devices",
]

#: §4.9/§7.6: значения по умолчанию, если ни конфиг, ни сервер их не задали
DEFAULT_CHECK_INTERVAL_SEC = 60
DEFAULT_PING_TIMEOUT_MS = 1000
#: Не больше 16 проверок одновременно (§7.6): касса — не сервер мониторинга
DEFAULT_CHECK_CONCURRENCY = 16
#: Сколько батчей ждёт недоступный сервер (буфер в памяти, §7.6)
MAX_BUFFERED_BATCHES = 20
#: Запас к таймауту `ping.exe`: сам процесс живёт дольше своего `-w`
PING_EXEC_MARGIN_SEC = 2.0
#: Порт по умолчанию для TCP-проверки, если он не задан (HTTP-камера)
DEFAULT_TCP_PORT = 80

PING_MISS_ERROR = "нет ответа на ping"
TCP_MISS_ERROR = "не удалось открыть TCP-соединение"
#: §5.2.2: батч результатов опроса оборудования подсети
DEVICES_STATUS_PATH = "/api/v1/agent/devices/status"


class CheckableDevice(BaseModel):
    """Устройство, которое агент умеет проверить (спека списка или запроса)."""

    ip: str
    check_type: CheckType = CheckType.ICMP
    port: int | None = None


def _as_checkable(spec: DeviceSpec | DeviceCheckSpec) -> CheckableDevice:
    """Свести `DeviceSpec`/`DeviceCheckSpec` к общей паре «IP + способ проверки»."""
    return CheckableDevice(ip=spec.ip, check_type=spec.check_type, port=spec.port)


class DevicesConfig(BaseModel):
    """Копия списка оборудования на кассе (`C:\\MagazAgent\\devices.json`, §7.6)."""

    model_config = ConfigDict(extra="ignore")

    devices_version: int = 1
    #: Д-9: расписание опроса — из настроек сервера (§4.9), не из кода агента
    check_interval_sec: int = DEFAULT_CHECK_INTERVAL_SEC
    ping_timeout_ms: int = DEFAULT_PING_TIMEOUT_MS
    devices: list[DeviceSpec] = Field(default_factory=list)

    def active(self) -> list[DeviceSpec]:
        """Устройства, которые агент обязан опрашивать (`is_active=true`)."""
        return [item for item in self.devices if item.is_active]


def load_devices(path: str | Path) -> DevicesConfig | None:
    """Прочитать копию списка; нет файла или он битый — `None` (опрос не падает)."""
    target = Path(path)
    if not target.is_file():
        return None
    try:
        with target.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        logger.warning("Копия списка оборудования не прочитана: %s", target)
        return None
    if not isinstance(data, dict):
        return None
    try:
        return DevicesConfig.model_validate(data)
    except ValueError:
        logger.warning("Копия списка оборудования повреждена: %s", target)
        return None


def save_devices(path: str | Path, config: DevicesConfig) -> Path:
    """Сохранить копию списка атомарно (temp + `replace`, как профиль 1С).

    Если кассу выключат в момент push-а `POST /devices/config`, файл останется
    читаемым: либо старый список, либо новый.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "devices_version": config.devices_version,
        "check_interval_sec": config.check_interval_sec,
        "ping_timeout_ms": config.ping_timeout_ms,
        "devices": [item.model_dump(mode="json") for item in config.devices],
    }

    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    tmp.replace(target)
    logger.debug(
        "Копия списка оборудования сохранена: %s (версия %s)", target, config.devices_version
    )
    return target


class DevicesStore:
    """Список оборудования, которым живёт агент: приём push-а и копия на диске."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        config: DevicesConfig | None = None,
    ) -> None:
        self.path = Path(path) if path else None
        self._config = config or DevicesConfig()

    @classmethod
    def load(
        cls,
        path: str | Path | None,
        *,
        interval_sec: int = DEFAULT_CHECK_INTERVAL_SEC,
        timeout_ms: int = DEFAULT_PING_TIMEOUT_MS,
    ) -> "DevicesStore":
        """Поднять список из копии на диске; её нет — пустой список (§7.6)."""
        target = Path(path) if path else None
        stored = load_devices(target) if target else None
        if stored is None:
            return cls(
                path=target,
                config=DevicesConfig(
                    check_interval_sec=max(1, int(interval_sec)),
                    ping_timeout_ms=max(1, int(timeout_ms)),
                ),
            )
        return cls(path=target, config=stored)

    @property
    def config(self) -> DevicesConfig:
        """Текущий список: версия, расписание и сами устройства."""
        return self._config

    @property
    def version(self) -> int:
        """Версия списка — её агент докладывает в `/health` (§7.6)."""
        return int(self._config.devices_version)

    @property
    def has_snapshot(self) -> bool:
        """Есть ли копия списка на диске (по ней `/health` не «врёт» после перезапуска)."""
        return self.path is not None and self.path.is_file()

    def active(self) -> list[DeviceSpec]:
        """Устройства к опросу: только `is_active=true` (выключение — soft-delete)."""
        return self._config.active()

    def apply_push(self, payload: DevicesConfigPush) -> DevicesConfig:
        """Принять push `POST /devices/config` и сохранить копию (§5.1.2, §7.6).

        Версия, интервал и таймаут берутся из push-а: сервер — источник истины
        (§4.9), поэтому расписание меняется в UI без правки конфига на кассе.
        Дубликаты IP схлопываются — в `store_devices` пара «магазин + IP» уникальна.
        """
        unique: dict[str, DeviceSpec] = {}
        for item in payload.devices:
            unique[item.ip] = item

        current = self._config
        self._config = DevicesConfig(
            devices_version=max(1, int(payload.devices_version or 1)),
            check_interval_sec=max(
                1, int(payload.check_interval_sec or current.check_interval_sec)
            ),
            ping_timeout_ms=max(1, int(payload.ping_timeout_ms or current.ping_timeout_ms)),
            devices=list(unique.values()),
        )
        if self.path is not None:
            save_devices(self.path, self._config)

        logger.info(
            "Список оборудования принят: версия %s, устройств %s (активных %s), интервал %s с",
            self._config.devices_version,
            len(self._config.devices),
            len(self._config.active()),
            self._config.check_interval_sec,
        )
        return self._config



class DeviceChecker:
    """Проверка устройств: `icmp` — `ping.exe` (argv, без shell), `tcp` — connect."""

    def __init__(
        self,
        *,
        provider: ProcessProvider = DEFAULT_PROVIDER,
        timeout_ms: int = DEFAULT_PING_TIMEOUT_MS,
        concurrency: int = DEFAULT_CHECK_CONCURRENCY,
    ) -> None:
        self.provider = provider
        self.timeout_ms = max(1, int(timeout_ms))
        self.concurrency = max(1, int(concurrency))

    def ping_args(self, ip: str) -> list[str]:
        """Команда ping списком аргументов — shell не участвует (§8.1, I-15)."""
        return ["ping.exe", "-n", "1", "-w", str(self.timeout_ms), ip]

    async def check(self, spec: DeviceSpec | DeviceCheckSpec) -> DeviceCheckResult:
        """Проверить одно устройство способом из его карточки (§4.11)."""
        device = _as_checkable(spec)
        if device.check_type == CheckType.TCP:
            return await self._check_tcp(device)
        return await self._check_icmp(device)

    async def check_many(
        self, specs: Sequence[DeviceSpec | DeviceCheckSpec]
    ) -> list[DeviceCheckResult]:
        """Проверить список устройств, не больше `concurrency` проверок разом (§7.6)."""
        if not specs:
            return []
        semaphore = asyncio.Semaphore(self.concurrency)

        async def guarded(spec: DeviceSpec | DeviceCheckSpec) -> DeviceCheckResult:
            async with semaphore:
                return await self.check(spec)

        return list(await asyncio.gather(*(guarded(spec) for spec in specs)))

    # --- Способы проверки ---------------------------------------------------

    async def _check_icmp(self, device: CheckableDevice) -> DeviceCheckResult:
        """`ping.exe -n 1 -w <мс> <ip>`: код `0` — ответ есть (§7.6)."""
        args = self.ping_args(device.ip)
        timeout_sec = self.timeout_ms / 1000 + PING_EXEC_MARGIN_SEC
        started = time.perf_counter()
        try:
            code = await self.provider.run_captured(args, timeout=timeout_sec)
        except FileNotFoundError:
            logger.warning("ping.exe недоступен: устройство %s проверить нечем", device.ip)
            return DeviceCheckResult(
                ip=device.ip, status=DeviceStatus.OFFLINE, error="ping.exe недоступен"
            )
        except OSError as exc:  # сбой запуска процесса, а не сети
            return DeviceCheckResult(
                ip=device.ip,
                status=DeviceStatus.OFFLINE,
                error=f"сбой запуска ping: {exc.__class__.__name__}",
            )

        latency_ms = self._elapsed_ms(started)
        if code == 0:
            return DeviceCheckResult(ip=device.ip, status=DeviceStatus.ONLINE, latency_ms=latency_ms)
        if code is None:
            return DeviceCheckResult(
                ip=device.ip, status=DeviceStatus.OFFLINE, error=PING_MISS_ERROR
            )
        # `ping.exe` отвечает кодом 1, когда ответа нет — это и есть «молчит»
        return DeviceCheckResult(
            ip=device.ip, status=DeviceStatus.OFFLINE, error=f"{PING_MISS_ERROR} (код {code})"
        )

    async def _check_tcp(self, device: CheckableDevice) -> DeviceCheckResult:
        """TCP-connect на порт устройства: `tcp` нужен камерам без ICMP (§7.6)."""
        port = int(device.port or DEFAULT_TCP_PORT)
        started = time.perf_counter()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(device.ip, port), timeout=self.timeout_ms / 1000
            )
        except (TimeoutError, asyncio.TimeoutError):
            return DeviceCheckResult(
                ip=device.ip, status=DeviceStatus.OFFLINE, error=f"{TCP_MISS_ERROR} (порт {port})"
            )
        except OSError as exc:
            return DeviceCheckResult(
                ip=device.ip,
                status=DeviceStatus.OFFLINE,
                error=f"{TCP_MISS_ERROR} (порт {port}, {exc.__class__.__name__})",
            )

        latency_ms = self._elapsed_ms(started)
        writer.close()
        with contextlib.suppress(Exception):  # закрытие «в никуда» не должно ронять цикл
            await writer.wait_closed()
        return DeviceCheckResult(ip=device.ip, status=DeviceStatus.ONLINE, latency_ms=latency_ms)

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        """Задержка ответа в миллисекундах (для `last_latency_ms`, §4.11)."""
        return max(0, int((time.perf_counter() - started) * 1000))



class DeviceStatusSender:
    """Досылка батча результатов на сервер (§5.2.2, §7.6).

    Агент ходит на сервер сам (`POST /api/v1/agent/devices/status`), поэтому
    httpx-клиент инжектируется: в тестах сеть подменяется `MockTransport` (§13).
    """

    def __init__(
        self,
        config: AgentConfig,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.config = config
        self._client = client
        self._transport = transport
        self.timeout = float(timeout)

    @property
    def url(self) -> str:
        """Полный адрес точки приёма батчей на сервере."""
        base = (self.config.server_url or "").rstrip("/")
        return f"{base}{DEVICES_STATUS_PATH}"

    async def send(self, batch: DeviceStatusBatch) -> bool:
        """Отправить батч; `False` — сервер не принял (батч остаётся в буфере)."""
        if not self.config.server_url or not self.config.agent_token:
            logger.debug("Адрес сервера или токен агента не заданы — результаты копим локально")
            return False

        headers = {"X-Agent-Token": self.config.agent_token, "Accept": "application/json"}
        payload = batch.model_dump(mode="json")
        try:
            if self._client is not None:
                response = await self._client.post(self.url, json=payload, headers=headers)
            else:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self.timeout),
                    transport=self._transport,
                    trust_env=False,
                ) as client:
                    response = await client.post(self.url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            logger.debug("Батч статусов не доставлен (%s): %s", self.url, exc.__class__.__name__)
            return False
        if response.status_code >= 400:
            logger.warning(
                "Сервер отклонил батч статусов: %s %s", response.status_code, response.text[:200]
            )
            return False
        return True



class DeviceMonitor:
    """Расписание опроса оборудования подсети (Д-1, §7.6).

    Один цикл: опросить активные устройства → положить батч в буфер → дослать
    всё, что накопилось. Пока сервер недоступен, батчи лежат в памяти (не больше
    `MAX_BUFFERED_BATCHES`), а следующий цикл сначала досылает их.
    """

    def __init__(
        self,
        store: DevicesStore,
        *,
        checker: DeviceChecker,
        sender: DeviceStatusSender,
        store_id: int,
        interval_override_sec: float | None = None,
        startup_delay_sec: float = 5.0,
        buffer_limit: int = MAX_BUFFERED_BATCHES,
    ) -> None:
        self.store = store
        self.checker = checker
        self.sender = sender
        self.store_id = int(store_id)
        self._interval_override = interval_override_sec
        self._startup_delay = max(0.0, float(startup_delay_sec))
        self._buffer: deque[DeviceStatusBatch] = deque(maxlen=max(1, int(buffer_limit)))
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        #: Последний снимок результатов — для ответа `/devices/check-now` и диагностики
        self.last_results: list[DeviceCheckResult] = []

    # --- Расписание ---------------------------------------------------------

    @property
    def interval_sec(self) -> float:
        """Интервал опроса: из push-а сервера (§4.9), иначе из конфига агента."""
        if self._interval_override is not None:
            return max(0.05, float(self._interval_override))
        return float(max(1, int(self.store.config.check_interval_sec)))

    @property
    def pending(self) -> int:
        """Сколько батчей ждёт недоступного сервера."""
        return len(self._buffer)

    @property
    def is_running(self) -> bool:
        """Фоновая задача запущена и ещё не завершилась."""
        return self._task is not None and not self._task.done()

    async def run(self) -> None:
        """Цикл «опросить → дослать → пауза» до сигнала остановки."""
        if self._startup_delay > 0:
            await self._sleep(self._startup_delay)
        while not self._stop_event.is_set():
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 - опрос не должен ронять службу
                logger.exception("Сбой цикла опроса оборудования подсети")
            await self._sleep(self.interval_sec)

    async def _sleep(self, seconds: float) -> None:
        """Пауза, прерываемая остановкой службы."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=max(0.0, seconds))
        except (TimeoutError, asyncio.TimeoutError):
            pass

    def start(self) -> asyncio.Task[None]:
        """Запустить опрос (идемпотентно)."""
        if self.is_running:
            assert self._task is not None
            return self._task
        self._stop_event.clear()
        self._task = asyncio.get_running_loop().create_task(self.run(), name="devices-monitor")
        return self._task

    async def stop(self) -> None:
        """Остановить опрос и дождаться завершения задачи."""
        self._stop_event.set()
        task, self._task = self._task, None
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (TimeoutError, asyncio.TimeoutError):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


    # --- Один цикл ----------------------------------------------------------

    async def poll_once(self) -> list[DeviceCheckResult]:
        """Опросить активные устройства и дослать накопленные батчи (§7.6)."""
        results = await self.checker.check_many(self.store.active())
        self.last_results = results
        if results:
            self._buffer.append(DeviceStatusBatch(store_id=self.store_id, devices=results))
        if results or self._buffer:
            logger.debug(
                "Опрос оборудования: устройств %s, батчей в буфере %s",
                len(results),
                self.pending,
            )
        await self.flush()
        return results

    async def flush(self) -> int:
        """Дослать буфер; ошибка отправки останавливает проход, батчи не теряются."""
        sent = 0
        while self._buffer:
            if not await self.sender.send(self._buffer[0]):
                break
            self._buffer.popleft()
            sent += 1
        return sent

    async def check_now(
        self, specs: Sequence[DeviceSpec | DeviceCheckSpec]
    ) -> list[DeviceCheckResult]:
        """Внеочередная проверка по запросу сервера (§5.1.2).

        Ответ синхронный: результат этого же запроса и есть ответ серверу, в
        буфер он не попадает — запись в `store_devices` делает сервер (§5.2.2).
        """
        return await self.checker.check_many(list(specs))

