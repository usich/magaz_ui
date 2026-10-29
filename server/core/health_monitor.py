"""Health Monitor — фоновая задача опроса агентов (§3.2, §3.3, §12 Э2).

Правила из спецификации, которые здесь и живут:

* опрос идёт **параллельно** с семафором `health_concurrency` (по умолчанию 20)
  и таймаутом `health_timeout_sec` (5 с) на магазин;
* успешный `GET /health` пишет в `stores` значение `last_seen_at` и сведения
  самодоклада (`agent_version`, `os_info`);
* признак «онлайн» **не хранится**: он вычисляется по окну `online_window_sec`
  (45 с) относительно `last_seen_at` (`Store.is_online`), поэтому не может
  рассинхронизироваться с фактом;
* недоступный магазин — не ошибка сервера: результат цикла помечает его
  «офлайн», в лог уходит одна строка на магазин, журнал не «залипает».

Опрашиваются только активные магазины (`is_active=false` — «исключение из
мониторинга и массовых операций», §4.1).

Тесты подменяют сеть транспортом `httpx.MockTransport`, поэтому ни касса, ни
туннель для проверок не нужны (§13, I-11).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from server.config import Settings
from server.core.client_http import (
    AgentAuthError,
    AgentClient,
    AgentError,
    AgentOfflineError,
    AgentTarget,
    build_agent_target,
)
from server.db.models import Store
from server.db.session import DatabaseManager, commit_with_retry
from server.services import settings_service
from shared.schemas import HealthResponse
from shared.timeutil import utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "HealthMonitor",
    "MonitorCycleReport",
    "StoreHealthResult",
    "poll_store",
    "run_cycle",
]

#: Ключи настроек §4.9, от которых зависит мониторинг
INTERVAL_SETTING = "health_interval_sec"
TIMEOUT_SETTING = "health_timeout_sec"
CONCURRENCY_SETTING = "health_concurrency"
ONLINE_WINDOW_SETTING = "online_window_sec"

#: Фабрика клиента агента — точка подмены в тестах (I-11)
ClientFactory = Callable[[AgentTarget, float, httpx.AsyncBaseTransport | None], AgentClient]


@dataclass(slots=True)
class StoreHealthResult:
    """Результат одной проверки магазина (в БД не сохраняется, живёт в отчёте)."""

    store_id: int
    ok: bool
    checked_at: datetime
    latency_ms: int = 0
    health: HealthResponse | None = None
    error: str | None = None
    #: `offline` (нет ответа) | `auth` (401/403 от агента) | `protocol` (битый ответ)
    error_kind: str | None = None

    @property
    def online(self) -> bool:
        """Магазин онлайн по итогам этого цикла."""
        return self.ok and self.health is not None


@dataclass(slots=True)
class MonitorCycleReport:
    """Итог одного цикла мониторинга (диагностика, `/system/info`, тесты)."""

    started_at: datetime
    finished_at: datetime
    results: list[StoreHealthResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Сколько магазинов опрошено."""
        return len(self.results)

    @property
    def ok_count(self) -> int:
        """Сколько ответили."""
        return sum(1 for item in self.results if item.ok)

    @property
    def failed_count(self) -> int:
        """Сколько не ответили (офлайн или ошибка протокола)."""
        return self.total - self.ok_count

    @property
    def duration_ms(self) -> int:
        """Длительность цикла, мс."""
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    def as_dict(self) -> dict[str, Any]:
        """Краткая сводка для API/логов."""
        return {
            "started_at": self.started_at.isoformat() + "Z",
            "finished_at": self.finished_at.isoformat() + "Z",
            "duration_ms": self.duration_ms,
            "total": self.total,
            "ok": self.ok_count,
            "failed": self.failed_count,
        }


#: Сколько ждём завершения фоновой задачи при остановке сервера
STOP_TIMEOUT_SEC = 5.0


async def poll_store(
    target: AgentTarget,
    *,
    timeout: float,
    transport: httpx.AsyncBaseTransport | None = None,
    client_factory: ClientFactory | None = None,
    shared_client: httpx.AsyncClient | None = None,
) -> StoreHealthResult:
    """Опросить один магазин: `GET /health` с таймаутом.

    Любая ошибка обращения к агенту — это результат проверки («офлайн»), а не
    исключение наружу: мониторинг обязан переживать недоступные кассы.
    `shared_client` — общий httpx-клиент на весь цикл опроса (переиспользование
    соединений; см. `poll_targets`).
    """
    started = time.perf_counter()

    def elapsed_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        if client_factory is not None:
            client = client_factory(target, timeout, transport)
            async with client:
                health = await client.get_health()
        elif shared_client is not None:
            async with AgentClient(
                target, timeout=timeout, transport=transport, client=shared_client
            ) as client:
                health = await client.get_health()
        else:
            async with AgentClient(target, timeout=timeout, transport=transport) as client:
                health = await client.get_health()
    except AgentError as exc:
        error_kind = (
            "offline"
            if isinstance(exc, AgentOfflineError)
            else "auth"
            if isinstance(exc, AgentAuthError)
            else "protocol"
        )
        logger.warning(
            "Магазин id=%s: мониторинг не удался (%s) — %s",
            target.store_id,
            error_kind,
            exc.message,
        )
        return StoreHealthResult(
            store_id=target.store_id,
            ok=False,
            checked_at=utcnow(),
            latency_ms=elapsed_ms(),
            error=exc.message,
            error_kind=error_kind,
        )

    return StoreHealthResult(
        store_id=target.store_id,
        ok=True,
        checked_at=utcnow(),
        latency_ms=elapsed_ms(),
        health=health,
    )


async def poll_targets(
    targets: list[AgentTarget],
    *,
    timeout: float,
    concurrency: int,
    transport: httpx.AsyncBaseTransport | None = None,
    client_factory: ClientFactory | None = None,
) -> list[StoreHealthResult]:
    """Опросить список магазинов параллельно, не превышая `concurrency`.

    В бою (без `client_factory`) используется **один** общий `httpx.AsyncClient`
    на весь цикл: соединения переиспользуются, а не открываются заново на каждый
    магазин. На Windows это резко снижает число исходящих сбоев к недоступным
    хостам, из-за которых asyncio Proactor «застревал» на приёме входящих
    соединений и админка «провисала».
    """
    if not targets:
        return []

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def guarded(target: AgentTarget) -> StoreHealthResult:
        async with semaphore:
            return await poll_store(
                target,
                timeout=timeout,
                transport=transport,
                client_factory=client_factory,
                shared_client=shared_client,
            )

    if client_factory is not None:
        # Тесты: каждый запрос через свою фабрику (подменённый транспорт).
        shared_client = None
        return list(await asyncio.gather(*(guarded(target) for target in targets)))

    # Боевой путь: один общий клиент на весь цикл опроса.
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout), trust_env=False, transport=transport
    ) as shared_client:
        return list(await asyncio.gather(*(guarded(target) for target in targets)))


def _load_targets(
    db: DatabaseManager,
    settings: Settings,
) -> tuple[list[AgentTarget], float, int]:
    """Прочитать настройки мониторинга и активные магазины из БД (синхронно, в потоке).

    Вынесено из event loop: открытие сессии и чтение не должны блокировать uvicorn,
    когда БД залочена пишущим циклом (health/диспетчер).
    """
    with db.session() as session:
        timeout = float(settings_service.get_int(session, TIMEOUT_SETTING))
        concurrency = settings_service.get_int(session, CONCURRENCY_SETTING)
        stores = list(
            session.scalars(
                select(Store).where(Store.is_active.is_(True)).order_by(Store.id)
            ).all()
        )
        targets = [build_agent_target(store, settings) for store in stores]
    return targets, timeout, concurrency


def _write_self_reports(db: DatabaseManager, results: list[StoreHealthResult]) -> None:
    """Записать самодоклады (last_seen_at, версии) и закоммитить — в потоке.

    `commit_with_retry` при «database is locked» ждёт busy_timeout и спал с
    `time.sleep` — синхронно. В event loop это блокировало бы весь uvicorn
    («админка виснет», включая `/health`), поэтому выполняем в отдельном потоке.
    """
    with db.session() as session:
        try:
            for result in results:
                health = result.health
                if health is None:
                    continue
                store = session.get(Store, result.store_id)
                if store is None:  # pragma: no cover - магазин удалён во время цикла
                    continue
                store.last_seen_at = result.checked_at
                store.agent_version = health.agent_version
                store.os_info = health.os
            commit_with_retry(session)
        except OperationalError:
            # Блокировка файла БД (параллельно пишут диспетчер/API): откатываем,
            # чтобы сессия не осталась в PendingRollbackError, и пропускаем
            # запись самодокладов — следующий цикл всё повторит.
            session.rollback()
            logger.warning("Запись самодокладов пропущена: БД заблокирована")


async def run_cycle(
    db: DatabaseManager,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    client_factory: ClientFactory | None = None,
) -> MonitorCycleReport:
    """Один цикл мониторинга: опрос + запись самодокладов в `stores`.

    Настройки (таймаут, параллелизм) читаются из БД на каждом цикле — изменение
    в UI применяется без перезапуска сервера (§4.9). Чтение и запись БД вынесены
    в потоки (`asyncio.to_thread`): блокирующий синхронный commit не должен
    замораживать event loop uvicorn, иначе админка и `/health` виснут.
    """
    started_at = utcnow()

    targets, timeout, concurrency = await asyncio.to_thread(_load_targets, db, settings)

    results = await poll_targets(
        targets,
        timeout=timeout,
        concurrency=concurrency,
        transport=transport,
        client_factory=client_factory,
    )

    await asyncio.to_thread(_write_self_reports, db, results)

    report = MonitorCycleReport(started_at=started_at, finished_at=utcnow(), results=results)
    logger.debug("Цикл мониторинга: %s", report.as_dict())
    return report


class HealthMonitor:
    """Фоновая задача опроса агентов с корректным стартом и остановкой.

    Создаётся в `lifespan` приложения (`server/main.py`) и живёт ровно столько,
    сколько живёт сервер: фоновых задач на уровне модуля нет (I-10).
    """

    def __init__(
        self,
        db: DatabaseManager,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        interval_sec: int | None = None,
        startup_delay_sec: float | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._transport = transport
        self._interval_override = interval_sec
        self._startup_delay = (
            settings.health_monitor_startup_delay_sec
            if startup_delay_sec is None
            else startup_delay_sec
        )
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_report: MonitorCycleReport | None = None

    # --- Настройки из БД ----------------------------------------------------

    def _setting(self, key: str) -> int:
        with self._db.session() as session:
            return settings_service.get_int(session, key)

    @property
    def interval_sec(self) -> int:
        """Интервал между циклами: настройка §4.9 (или значение из теста)."""
        if self._interval_override is not None:
            return max(1, int(self._interval_override))
        return max(1, self._setting(INTERVAL_SETTING))

    @property
    def online_window_sec(self) -> int:
        """Окно, в котором магазин считается онлайн (§3.3)."""
        return max(1, self._setting(ONLINE_WINDOW_SETTING))

    @property
    def is_running(self) -> bool:
        """Задача запущена и ещё не завершилась."""
        return self._task is not None and not self._task.done()

    # --- Циклы и жизненный цикл --------------------------------------------

    async def poll_once(self) -> MonitorCycleReport:
        """Выполнить один цикл мониторинга немедленно (кнопка «Обновить», тесты)."""
        report = await run_cycle(self._db, self._settings, transport=self._transport)
        self.last_report = report
        return report

    async def run(self) -> None:
        """Цикл «опрос → пауза на интервал» до сигнала остановки."""
        if self._startup_delay > 0:
            await self._sleep(self._startup_delay)

        while not self._stop_event.is_set():
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 - мониторинг не падает от одной ошибки
                logger.exception("Сбой цикла мониторинга")
            await self._sleep(self.interval_sec)

        logger.debug("Health Monitor остановлен")

    async def _sleep(self, seconds: float) -> None:
        """Пауза, прерываемая остановкой сервера (без «залипания» на shutdown)."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=max(0.0, seconds))
        except (TimeoutError, asyncio.TimeoutError):
            pass

    def start(self) -> asyncio.Task[None]:
        """Запустить фоновую задачу (идемпотентно)."""
        if self.is_running and self._task is not None:
            return self._task
        self._stop_event.clear()
        self._task = asyncio.create_task(self.run(), name="magaz-health-monitor")
        logger.info(
            "Health Monitor запущен: интервал %s с, окно «онлайн» %s с",
            self.interval_sec,
            self.online_window_sec,
        )
        return self._task

    async def stop(self) -> None:
        """Остановить задачу и дождаться её завершения."""
        self._stop_event.set()
        task, self._task = self._task, None
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(task, timeout=STOP_TIMEOUT_SEC)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(
                "Health Monitor не завершился за %s с — отменяем задачу", STOP_TIMEOUT_SEC
            )
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
