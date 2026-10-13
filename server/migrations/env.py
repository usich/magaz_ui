"""Alembic: подключение метаданных моделей §4 и строки подключения из настроек.

Строка подключения берётся из `server/config.py` (переменная `MAGAZ_DATABASE_URL`),
поэтому `alembic.ini` править не нужно: и сервер, и миграции смотрят в одну БД.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Корень репозитория в sys.path — чтобы `import server...` работал при запуске
# `alembic` из любого каталога
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.config import get_settings  # noqa: E402
from server.db.base import Base  # noqa: E402
from server.db import models  # noqa: E402,F401 - регистрация таблиц в metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_database_url() -> str:
    """URL из настроек приложения (единая точка правды)."""
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Генерация SQL без подключения к БД (`alembic upgrade head --sql`)."""
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Применение миграций к реальной БД."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_database_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite не умеет ALTER COLUMN — batch-режим делает пересоздание таблицы
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
