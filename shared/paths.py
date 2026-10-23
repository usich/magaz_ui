"""Безопасная работа с путями и именами файлов (инварианты I-2, I-15).

Общее для сервера и агента: имя скачанного файла — всегда basename, записи ZIP
не должны выходить за рабочую папку (Zip Slip).
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath

__all__ = [
    "UPDATE_FILE_SUFFIXES",
    "is_extension_merge",
    "is_extension_operation",
    "is_safe_archive_member",
    "safe_basename",
    "split_merge_file_name",
    "validate_file_name",
    "validate_ipv4_address",
]

#: Расширения файлов обновления (§4.5, Э10): только их можно удалять на кассе
UPDATE_FILE_SUFFIXES: tuple[str, ...] = (".cf", ".cfe", ".xml")

_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


def validate_file_name(value: str) -> str:
    """Имя файла без расширения: без путей, без `.`/`..` (перенос v2)."""
    value = (value or "").strip()
    if not value:
        raise ValueError("Имя файла не может быть пустым")
    if value in (".", "..") or "/" in value or "\\" in value:
        raise ValueError("Имя файла не должно содержать разделителей пути")
    return value


def split_merge_file_name(value: str) -> tuple[str, str | None]:
    """Разобрать имя файла слияния на «корень» и явное расширение (FR-4, Д-16).

    Пользователь может указать имя с расширением (`1Cv8-mag-2110.cf` / `.cfe`) —
    тогда тип слияния определяется по файлу, а не по `extension_name` из карточки.
    Возвращает `(stem, suffix)`: `suffix` равен `".cf"`/`".cfe"`, если расширение
    указано явно, иначе `None` (тип тогда решает `extension_name`). Регистр
    расширения не важен, порядок проверки — `.cfe` раньше `.cf` (иначе `.cfe`
    был бы ошибочно принят за `.cf`).
    """
    raw = (value or "").strip()
    lower = raw.lower()
    for suffix in (".cfe", ".cf"):
        if lower.endswith(suffix):
            return raw[: len(raw) - len(suffix)], suffix
    return raw, None


def is_extension_merge(file_name: str, extension_name: str | None) -> bool:
    """Сливается ли **расширение** (а не основная конфигурация) (FR-4, Д-16).

    Приоритет у фактического файла, а не у карточки магазина:
    * имя оканчивается на `.cfe` → расширение (`True`), даже если `extension_name`
      не задан (значение для `-Extension` возьмётся из карточки);
    * имя оканчивается на `.cf` → основная конфигурация (`False`), даже если в
      карточке магазина прописано расширение — это и есть способ обновить `.cf`
      на магазине с настроенным расширением;
    * без расширения → по `extension_name` (есть → расширение, нет → основная).
    """
    _, suffix = split_merge_file_name(file_name)
    if suffix == ".cfe":
        return True
    if suffix == ".cf":
        return False
    return bool(extension_name)


def is_extension_operation(
    is_extension: bool | None, file_name: str, extension_name: str | None
) -> bool:
    """Обновляем ли **расширение** (Д-17): явный флаг операции главнее остального.

    Приоритет: заданный `is_extension` → расширение в имени файла (`.cf`/`.cfe`)
    → `extension_name` (параметры или карточка магазина). Это единый источник
    «что обновляем» для `/run-merge` и `/run-update-db`: массовая операция передаёт
    явный флаг, а не полагается на карточку магазина.
    """
    if is_extension is not None:
        return bool(is_extension)
    return is_extension_merge(file_name, extension_name)


def safe_basename(name: str) -> str:
    """Взять безопасный basename из имени файла (FR-1, I-2).

    Отсекает и POSIX-, и Windows-разделители: имя приходит из внешнего URL и
    может содержать `..\\..\\evil.exe`.
    """
    raw = (name or "").strip().strip('"')
    if not raw:
        raise ValueError("Пустое имя файла")
    candidate = PurePosixPath(PureWindowsPath(raw).name).name
    if candidate in ("", ".", ".."):
        raise ValueError("Недопустимое имя файла")
    return candidate


def is_safe_archive_member(member: str) -> bool:
    """Проверка записи ZIP-архива на Zip Slip (I-2).

    Разрешены только относительные пути внутри архива без `..`.
    """
    raw = (member or "").strip().replace("\\", "/")
    if not raw:
        return False
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return False
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts:
        return False
    return all(part != ".." for part in parts)


def validate_ipv4_address(value: str) -> str:
    """Проверить, что это IPv4-адрес (для `store_devices.ip_address`, I-15).

    Доменные имена и диапазоны не принимаем: агент проверяет только явные IP.
    """
    raw = (value or "").strip()
    if not _IPV4_RE.match(raw):
        raise ValueError("Ожидается IPv4-адрес, например 10.10.5.21")
    octets = raw.split(".")
    if any(int(octet) > 255 for octet in octets):
        raise ValueError("Недопустимый IPv4-адрес")
    return raw
