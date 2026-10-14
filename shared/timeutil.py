"""Работа со временем (единая точка для сервера и агента).

Все временные метки в системе — **UTC, naive** (без tzinfo): SQLite хранит
DATETIME как строку без часового пояса, и смешивание aware/naive значений
приводит к трудноуловимым ошибкам сравнения. Наружу (в API) отдаём ISO-8601
с суффиксом `Z`, чтобы клиент однозначно понимал зону.
"""

from datetime import datetime, timezone

__all__ = ["parse_iso_z", "utcnow", "to_iso_z"]


def utcnow() -> datetime:
    """Текущее время UTC без tzinfo (единый формат для БД)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_iso_z(value: datetime | None) -> str | None:
    """Преобразовать метку БД (UTC, naive) в ISO-8601 с суффиксом `Z`."""
    if value is None:
        return None
    return value.replace(microsecond=0).isoformat() + "Z"


def parse_iso_z(value: str | None) -> datetime | None:
    """Разобрать ISO-8601 (с `Z` или смещением) в метку БД: UTC, naive (§6).

    Нужна для полей, которые приходят строками: дедлайн уведомления из UI и
    `snooze_until` из очереди агента. Битое значение — `None`, а не исключение:
    неверный формат одной отметки не должен ронять приём отчёта (§7.4).
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = None
    for candidate in (text, text.replace("Z", "+00:00"), text.replace("Z", "")):
        try:
            parsed = datetime.fromisoformat(candidate)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
