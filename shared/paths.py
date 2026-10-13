"""Безопасная работа с путями и именами файлов (инварианты I-2, I-15).

Общее для сервера и агента: имя скачанного файла — всегда basename, записи ZIP
не должны выходить за рабочую папку (Zip Slip).
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath

__all__ = [
    "is_safe_archive_member",
    "safe_basename",
    "validate_file_name",
    "validate_ipv4_address",
]

_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


def validate_file_name(value: str) -> str:
    """Имя файла без расширения: без путей, без `.`/`..` (перенос v2)."""
    value = (value or "").strip()
    if not value:
        raise ValueError("Имя файла не может быть пустым")
    if value in (".", "..") or "/" in value or "\\" in value:
        raise ValueError("Имя файла не должно содержать разделителей пути")
    return value


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
