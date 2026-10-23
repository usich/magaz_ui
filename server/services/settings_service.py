"""Настройки сервера (§4.9): значения по умолчанию, чтение, валидация, запись.

Все ключи — целые, кроме `default_file_name` (имя файла обновления по умолчанию).
Настройки читаются из БД, поэтому меняются из UI без перезапуска сервера.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.db.models import Setting
from shared.timeutil import utcnow

__all__ = [
    "DEFAULT_SETTINGS",
    "INT_SETTING_KEYS",
    "ensure_defaults",
    "get_all",
    "get_int",
    "get_str",
    "set_many",
    "validate_value",
]

#: §4.9 — «Ключ / По умолчанию»
DEFAULT_SETTINGS: dict[str, str] = {
    "health_interval_sec": "15",
    "health_timeout_sec": "5",
    "online_window_sec": "45",
    "health_concurrency": "20",
    "check_poll_interval_sec": "5",
    "default_file_name": "update",
    "command_retry_max": "3",
    "log_excerpt_limit_bytes": "204800",
    # Д-7 (Э4): предел ожидания операции 1С и период сверки учётных данных (П-1)
    "command_poll_timeout_sec": "3600",
    "credentials_push_interval_sec": "300",
    "notification_snooze_min": "15",
    "device_check_interval_sec": "60",
    "device_online_window_sec": "180",
    "device_ping_timeout_ms": "1000",
    # Д-21: проверка бэкапов 1С — интервал обхода, час начала запросов (10:00),
    # лимит попыток в день и интервал между повторными попытками (10 мин)
    "backup_interval_sec": "60",
    "backup_start_hour": "10",
    "backup_max_tries": "5",
    "backup_retry_interval_sec": "600",
}

#: Ключи, значение которых обязано быть неотрицательным целым
INT_SETTING_KEYS: frozenset[str] = frozenset(DEFAULT_SETTINGS) - {"default_file_name"}


def validate_value(key: str, value: str) -> str:
    """Проверить ключ и значение настройки; вернуть нормализованное значение."""
    if key not in DEFAULT_SETTINGS:
        raise ValueError(f"Неизвестная настройка: {key}")

    raw = str(value).strip()
    if not raw:
        raise ValueError(f"Значение настройки {key} не может быть пустым")

    if key in INT_SETTING_KEYS:
        try:
            number = int(raw)
        except ValueError as exc:
            raise ValueError(f"Настройка {key} должна быть целым числом") from exc
        if number < 0:
            raise ValueError(f"Настройка {key} не может быть отрицательной")
        return str(number)

    if "/" in raw or "\\" in raw or raw in (".", ".."):
        raise ValueError("Имя файла по умолчанию не должно содержать разделителей пути")
    return raw


def ensure_defaults(session: Session) -> int:
    """Добавить отсутствующие настройки из §4.9; вернуть число добавленных."""
    existing = set(session.scalars(select(Setting.key)).all())
    added = 0
    for key, value in DEFAULT_SETTINGS.items():
        if key not in existing:
            session.add(Setting(key=key, value=value))
            added += 1
    if added:
        session.flush()
    return added


def get_all(session: Session) -> dict[str, str]:
    """Все настройки словарём `{ключ: значение}`."""
    return {row.key: row.value for row in session.scalars(select(Setting)).all()}


def get_str(session: Session, key: str) -> str:
    """Значение настройки строкой (при отсутствии — значение по умолчанию §4.9)."""
    row = session.get(Setting, key)
    if row is None:
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"Неизвестная настройка: {key}")
        return DEFAULT_SETTINGS[key]
    return row.value


def get_int(session: Session, key: str) -> int:
    """Целочисленное значение настройки (для интервалов мониторинга)."""
    if key not in INT_SETTING_KEYS:
        raise ValueError(f"Настройка {key} не является целочисленной")
    return int(get_str(session, key))


def set_value(session: Session, key: str, value: str | int) -> Setting:
    """Записать настройку с валидацией; `commit` — за вызывающим кодом."""
    normalized = validate_value(key, str(value))
    row = session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=normalized)
        session.add(row)
    else:
        row.value = normalized
        row.updated_at = utcnow()
    session.flush()
    return row

