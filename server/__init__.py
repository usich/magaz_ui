"""Сервер дашборда «Magaz UI» (FastAPI + SQLAlchemy 2.0 + Alembic).

Структура — §6 спецификации: `api/`, `core/`, `db/`, `services/`, `migrations/`,
данные и секреты — в `server/data/`.
"""

from shared.version import APP_NAME, APP_VERSION

__all__ = ["APP_NAME", "APP_VERSION"]
