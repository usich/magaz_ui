"""Тесты Health Monitor (§3.2, §3.3, §13.2): успех / таймаут / ошибка и окно 45 с.

Сеть подменяется `httpx.MockTransport`-ом (`tests/server/agent_mock.py`), поэтому
проверки не зависят ни от кассы, ни от WireGuard-туннеля (§13).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from server.config import Settings
from server.core.health_monitor import HealthMonitor, run_cycle
from server.db.models import Store
from server.db.session import DatabaseManager
from server.services import settings_service
from shared.timeutil import utcnow
from tests.server.agent_mock import (
    ConcurrencyProbe,
    health_body,
    ok_transport,
    status_transport,
    timeout_transport,
)

WINDOW_SEC = 45


def read_store(engine: Engine, store_id: int) -> Store:
    """Свежий снимок магазина из БД."""
    with Session(engine) as check:
        return check.get(Store, store_id)


def add_stores(session: Session, *, count: int, first_index: int) -> None:
    """Добавить активные магазины (проверки параллелизма опроса)."""
    for index in range(count):
        number = first_index + index
        session.add(
            Store(
                name=f"Магазин №{number}",
                ip_address=f"10.10.{number}.{number}",
                cluster_name="srv-1c",
                database_name="trade",
                version_1c="8.3.24.1548",
                user_name_1c="Администратор",
                agent_token=f"token-{number}",
            )
        )


class TestSingleCycle:
    """Один цикл опроса: что попадает в `stores` и в отчёт."""

    async def test_success_updates_self_report(
        self,
        settings: Settings,
        db_manager: DatabaseManager,
        store,  # noqa: ANN001
        engine: Engine,
    ) -> None:
        transport = ok_transport(
            health_body(store_id=store.id, agent_version="1.2.3", os="Windows 11 Pro")
        )

        report = await run_cycle(db_manager, settings, transport=transport)

        assert report.total == 1
        assert report.ok_count == 1
        assert report.failed_count == 0
        assert report.results[0].online is True
        assert report.results[0].latency_ms >= 0

        refreshed = read_store(engine, store.id)
        assert refreshed.agent_version == "1.2.3"
        assert refreshed.os_info == "Windows 11 Pro"
        assert refreshed.last_seen_at is not None
        assert refreshed.is_online(window_sec=WINDOW_SEC) is True

    async def test_timeout_keeps_store_offline(
        self,
        settings: Settings,
        db_manager: DatabaseManager,
        store,  # noqa: ANN001
        engine: Engine,
    ) -> None:
        report = await run_cycle(db_manager, settings, transport=timeout_transport())

        assert report.ok_count == 0
        assert report.failed_count == 1
        result = report.results[0]
        assert result.ok is False
        assert result.error_kind == "offline"
        assert result.error

        refreshed = read_store(engine, store.id)
        assert refreshed.last_seen_at is None, "неудачный опрос не двигает last_seen_at"
        assert refreshed.is_online(window_sec=WINDOW_SEC) is False

    async def test_agent_rejecting_token_is_reported_as_auth_error(
        self, settings: Settings, db_manager: DatabaseManager, store  # noqa: ANN001
    ) -> None:
        report = await run_cycle(
            db_manager, settings, transport=status_transport(401, {"detail": "нет"})
        )
        assert report.results[0].error_kind == "auth"

    async def test_broken_payload_is_protocol_error(
        self, settings: Settings, db_manager: DatabaseManager, store  # noqa: ANN001
    ) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"нет": "полей"}))
        report = await run_cycle(db_manager, settings, transport=transport)
        assert report.results[0].error_kind == "protocol"


class TestOnlineWindow:
    """«Онлайн» — производная от `last_seen_at` и окна 45 с (§3.3)."""

    async def test_store_goes_offline_after_window(
        self,
        settings: Settings,
        db_manager: DatabaseManager,
        store,  # noqa: ANN001
        engine: Engine,
    ) -> None:
        with Session(engine) as change:
            change.get(Store, store.id).last_seen_at = utcnow() - timedelta(
                seconds=WINDOW_SEC + 5
            )
            change.commit()

        assert read_store(engine, store.id).is_online(window_sec=WINDOW_SEC) is False

        report = await run_cycle(db_manager, settings, transport=timeout_transport())
        assert report.results[0].online is False

    async def test_recent_last_seen_keeps_store_online(
        self,
        settings: Settings,
        db_manager: DatabaseManager,
        store,  # noqa: ANN001
        engine: Engine,
    ) -> None:
        with Session(engine) as change:
            change.get(Store, store.id).last_seen_at = utcnow() - timedelta(seconds=10)
            change.commit()

        assert read_store(engine, store.id).is_online(window_sec=WINDOW_SEC) is True


class TestSelectionAndConcurrency:
    """Опрашиваются только активные магазины, параллелизм ограничен (§3.2, §4.1)."""

    async def test_inactive_store_is_not_polled(
        self,
        settings: Settings,
        db_manager: DatabaseManager,
        store,  # noqa: ANN001
        session: Session,
    ) -> None:
        probe = ConcurrencyProbe()
        store.is_active = False
        session.commit()

        report = await run_cycle(db_manager, settings, transport=probe.transport)

        assert report.total == 0
        assert probe.calls == 0, "магазин исключён из мониторинга (§4.1)"

    async def test_health_concurrency_limits_parallel_requests(
        self,
        settings: Settings,
        db_manager: DatabaseManager,
        store,  # noqa: ANN001
        session: Session,
    ) -> None:
        add_stores(session, count=2, first_index=20)
        settings_service.set_value(session, "health_concurrency", "1")
        session.commit()

        probe = ConcurrencyProbe(delay_sec=0.02)
        report = await run_cycle(db_manager, settings, transport=probe.transport)

        assert report.total == 3, "магазинов 3, все активные"
        assert probe.calls == 3
        assert probe.max_active == 1, "семафор health_concurrency держит лимит"

    async def test_all_requests_run_in_parallel_with_high_limit(
        self,
        settings: Settings,
        db_manager: DatabaseManager,
        store,  # noqa: ANN001
        session: Session,
    ) -> None:
        add_stores(session, count=4, first_index=30)
        settings_service.set_value(session, "health_concurrency", "20")
        session.commit()

        probe = ConcurrencyProbe(delay_sec=0.02)
        await run_cycle(db_manager, settings, transport=probe.transport)

        assert probe.max_active == 5, "лимит 20 больше числа магазинов — опрос параллельный"


class TestMonitorLifecycle:
    """Фоновая задача: чтение настроек из БД, запуск и корректная остановка."""

    async def test_poll_once_and_settings(
        self, settings: Settings, db_manager: DatabaseManager, store, session: Session  # noqa: ANN001
    ) -> None:
        settings_service.set_value(session, "health_interval_sec", "3")
        settings_service.set_value(session, "online_window_sec", "7")
        session.commit()

        monitor = HealthMonitor(db_manager, settings, transport=ok_transport())
        report = await monitor.poll_once()

        assert monitor.interval_sec == 3, "интервал читается из настроек §4.9"
        assert monitor.online_window_sec == 7
        assert monitor.last_report is report
        assert report.ok_count == 1

    async def test_start_and_stop(
        self, settings: Settings, db_manager: DatabaseManager, store  # noqa: ANN001
    ) -> None:
        monitor = HealthMonitor(db_manager, settings, transport=ok_transport(), interval_sec=1)
        assert monitor.is_running is False

        monitor.start()
        assert monitor.is_running is True
        await asyncio.sleep(0.05)

        await monitor.stop()
        assert monitor.is_running is False
        assert monitor.last_report is not None

    async def test_start_is_idempotent(
        self, settings: Settings, db_manager: DatabaseManager, store  # noqa: ANN001
    ) -> None:
        monitor = HealthMonitor(db_manager, settings, transport=ok_transport(), interval_sec=1)
        first = monitor.start()
        second = monitor.start()

        assert first is second
        await monitor.stop()

    async def test_failure_in_cycle_does_not_kill_monitor(
        self, settings: Settings, db_manager: DatabaseManager, store  # noqa: ANN001
    ) -> None:
        monitor = HealthMonitor(db_manager, settings, transport=timeout_transport(), interval_sec=1)
        monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()

        assert monitor.last_report is not None
        assert monitor.last_report.failed_count == 1, "офлайн-магазин не ломает мониторинг"