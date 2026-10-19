"""Программное применение Alembic-миграций при старте сервера.

Проблема, которую решает модуль: сервер проверял только наличие таблиц
(`server.main._assert_schema_ready`), но не свежесть колонок. Если к рабочей БД
не была применена миграция (например, `f2b7c9d31a04` добавила
`notifications.category_id`), сервер поднимался и падал на первом запросе с
`no such column`. Автоприменение в `lifespan` делает миграции идемпотентным шагом
запуска: `alembic upgrade head` ничего не делает, если схема уже на head.

Включается настройкой `Settings.auto_migrate` (`MAGAZ_AUTO_MIGRATE`). В тестах
выключено (`tests/server/conftest.py`): там схема создаётся `Base.metadata.create_all`.
"""

from __future__ import annotations

import logging

from alembic import command
from alembic.config import Config

from server.config import MIGRATIONS_DIR, Settings

logger = logging.getLogger(__name__)

__all__ = ["run_migrations"]


def run_migrations(settings: Settings) -> None:
    """Применить все ожидающие миграции Alembic к БД настроек.

    Идемпотентно: если ревизия БД уже `head`, `upgrade` ничего не меняет.
    URL передаётся через `magaz.url_override` — его приоритизирует
    `server/migrations/env.py` (`get_database_url`), поэтому миграции идут
    именно в ту БД, что использует приложение, независимо от глобальных
    настроек процесса.
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("magaz.url_override", settings.database_url)
    command.upgrade(config, "head")
