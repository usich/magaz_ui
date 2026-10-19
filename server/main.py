"""Точка входа сервера дашборда (§14.1): `python -m server.main`.

Порядок запуска:
1. читаются настройки (`server/config.py`, переменные `MAGAZ_*`);
2. создаются рабочие каталоги и ключи секретов (§8.3) — в `lifespan`;
3. проверяется, что схема БД на месте (её создаёт `alembic upgrade head`);
4. подсеиваются настройки по умолчанию §4.9;
5. подключаются роутеры: `/api/v1/admin/*`, `/api/v1/agent/*` и отдача SPA.

Фоновые задачи (Health Monitor, Dispatcher, Devices Monitor) появляются здесь
же — на этапах Э2, Э4 и Э4Б.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from server.api import admin as admin_api
from server.api import agent_report, spa
from server.config import API_PREFIX, Settings, get_settings
from server.core.context import AppContext, build_app_context
from server.core.dispatcher import CommandDispatcher
from server.core.health_monitor import HealthMonitor
from server.core.logging_setup import configure_logging
from server.db.migrations import run_migrations
from server.services import settings_service
from shared.version import APP_NAME, APP_VERSION

logger = logging.getLogger(__name__)

__all__ = ["app", "create_app", "main"]

REQUIRED_TABLES = ("stores", "admin_users", "settings")


def _assert_schema_ready(context: AppContext) -> None:
    """Понятная ошибка вместо `no such table` из недр SQLAlchemy."""
    existing = set(inspect(context.db.engine).get_table_names())
    missing = [table for table in REQUIRED_TABLES if table not in existing]
    if missing:
        raise RuntimeError(
            "Схема БД не создана (нет таблиц: "
            + ", ".join(missing)
            + "). Выполните: alembic upgrade head"
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Собрать ресурсы приложения, поднять фоновые задачи и корректно их закрыть."""
    settings: Settings = getattr(app.state, "settings", None) or get_settings()
    settings.ensure_directories()
    configure_logging(settings)

    # Транспорт к агентам: в проде — реальная сеть, в тестах подменяется моком (I-11)
    agent_transport = getattr(app.state, "agent_transport", None)
    context = build_app_context(settings, agent_transport=agent_transport)
    app.state.ctx = context
    app.state.settings = settings
    logger.info("%s v%s запускается; БД: %s", APP_NAME, settings.app_version, settings.database_url)

    monitor: HealthMonitor | None = None
    dispatcher: CommandDispatcher | None = None
    try:
        # Автоприменение миграций: идемпотентный `alembic upgrade head`. Защищает
        # от `no such column`, когда к рабочей БД ещё не подошла миграция, но
        # таблицы уже есть (иначе `_assert_schema_ready` пропустит, а первый
        # запрос упадёт). Отключается настройкой `MAGAZ_AUTO_MIGRATE=false`.
        if settings.auto_migrate:
            run_migrations(settings)
        _assert_schema_ready(context)
        with context.db.session() as session:
            added = settings_service.ensure_defaults(session)
            session.commit()
        if added:
            logger.info("Настройки по умолчанию добавлены: %s шт.", added)

        # Э2: Health Monitor — фоновый опрос `GET /health` всех активных магазинов
        if settings.health_monitor_enabled:
            monitor = HealthMonitor(context.db, settings, transport=agent_transport)
            monitor.start()
        app.state.health_monitor = monitor

        # Э4: диспетчер — отправка задач на кассы, опрос `/check-*`, retry и
        # доставка учётных данных 1С (П-1)
        if settings.dispatcher_enabled:
            dispatcher = CommandDispatcher(
                context.db,
                settings,
                transport=agent_transport,
                secret_box=context.secret_box,
            )
            dispatcher.start()
        app.state.dispatcher = dispatcher
        yield
    except SQLAlchemyError:  # pragma: no cover - нужен реальный сбой БД
        logger.exception("Ошибка БД при старте сервера")
        raise
    finally:
        if dispatcher is not None:
            await dispatcher.stop()
        if monitor is not None:
            await monitor.stop()
        context.dispose()
        logger.info("Сервер остановлен")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Собрать приложение FastAPI (используется в тестах и в `uvicorn`)."""
    resolved = settings or get_settings()
    application = FastAPI(
        title=f"{APP_NAME} — сервер дашборда",
        version=APP_VERSION,
        description="Управление агентами обновления 1С в магазинах: мониторинг, операции, уведомления.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    application.state.settings = resolved

    @application.get(f"{API_PREFIX}/health", tags=["health"], summary="Сервер жив")
    def health() -> dict[str, str | bool]:
        """Публичная проверка доступности сервера (без секретов и без БД)."""
        return {"status": "online", "app_name": APP_NAME, "app_version": APP_VERSION}

    application.include_router(admin_api.router)
    application.include_router(agent_report.router)

    if resolved.serve_spa:
        application.include_router(spa.build_spa_router(resolved))

    return application


def main() -> None:
    """Запуск uvicorn-ом: `python -m server.main`."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "server.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        log_level=settings.log_level.lower(),
    )


app = create_app()

if __name__ == "__main__":  # pragma: no cover - ручной запуск
    main()
