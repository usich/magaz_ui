"""Работа со временем (единая точка для сервера и агента).

Все временные метки в системе — **UTC, naive** (без tzinfo): SQLite хранит
DATETIME как строку без часового пояса, и смешивание aware/naive значений
приводит к трудноуловимым ошибкам сравнения. Наружу (в API) отдаём ISO-8601
с суффиксом `Z`, чтобы клиент однозначно понимал зону.
"""

from datetime import datetime, timezone

__all__ = ["utcnow", "to_iso_z"]


def utcnow() -> datetime:
    """Текущее время UTC без tzinfo (единый формат для БД)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_iso_z(value: datetime | None) -> str | None:
    """Преобразовать метку БД (UTC, naive) в ISO-8601 с суффиксом `Z`."""
    if value is None:
        return None
    return value.replace(microsecond=0).isoformat() + "Z"
