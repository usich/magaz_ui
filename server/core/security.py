"""Безопасность сервера: пароли, cookie, токены агентов, шифрование секретов.

Здесь собраны все «чувствительные» операции, чтобы правило I-7 (пароль никогда
не попадает в логи, `commands.params`, аудит и HTML) проверялось в одном месте:

* хеширование паролей администраторов — PBKDF2-HMAC-SHA256;
* подпись сессионной cookie администратора — `itsdangerous` (§4.2);
* генерация `agent_token` для магазинов (§4.1);
* Fernet-шифрование пароля 1С (`stores.password_enc`, П-1, §8.3);
* маскирование секретов для логов и представлений.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

logger = logging.getLogger(__name__)

__all__ = [
    "COOKIE_SALT",
    "MASK",
    "SecretBox",
    "SessionSigner",
    "generate_agent_token",
    "hash_password",
    "load_or_create_secret",
    "mask_mapping",
    "mask_secret",
    "verify_password",
]

#: Заменитель значения секрета в логах, аудите и представлениях
MASK = "***"

#: Соль подписи cookie администратора
COOKIE_SALT = "magaz-admin-session"

PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 390_000
SALT_BYTES = 16


# --- Пароли администраторов -------------------------------------------------


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Хеш пароля администратора в формате `pbkdf2_sha256$iter$salt$hash`."""
    if not password:
        raise ValueError("Пароль не может быть пустым")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        (
            PBKDF2_ALGORITHM,
            str(iterations),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, password_hash: str) -> bool:
    """Проверить пароль по хешу; на битый хеш отвечаем `False`, а не исключением."""
    try:
        algorithm, iterations_raw, salt_b64, digest_b64 = password_hash.split("$")
        if algorithm != PBKDF2_ALGORITHM:
            return False
        iterations = int(iterations_raw)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (AttributeError, TypeError, ValueError):
        return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def generate_agent_token() -> str:
    """Токен агента (`X-Agent-Token`, §4.1): 32 байта url-safe энтропии."""
    return secrets.token_urlsafe(32)


def mask_secret(value: Any) -> str:
    """Маска секрета для логов и представлений (I-7)."""
    return MASK if value else ""


def mask_mapping(
    data: dict[str, Any], keys: tuple[str, ...] = ("password", "password_enc")
) -> dict[str, Any]:
    """Копия словаря с замаскированными секретными полями (для `commands.params`)."""
    masked = dict(data)
    for key in keys:
        if key in masked and masked[key]:
            masked[key] = MASK
    return masked


def load_or_create_secret(path: Path, env_value: str | None = None) -> bytes:
    """Ключ из переменной окружения/настройки, иначе — файл (создаётся при первом запуске).

    §8.3: ключ шифрования паролей 1С (`server\\data\\secrets.key`) — отдельный от
    ключа подписи cookie. Права на файл ограничиваем владельцем (`0o600`).
    """
    if env_value:
        return env_value.encode("utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_bytes().strip()

    key = Fernet.generate_key()
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows/сетевые ФС
        logger.warning("Не удалось ограничить права на файл секрета: %s", path)
    logger.info("Создан новый файл секрета: %s", path)
    return key


@dataclass(slots=True)
class SecretBox:
    """Симметричное шифрование секретов приложения (пароль 1С, П-1)."""

    key: bytes
    _fernet: Fernet = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.key, str):  # защита от случайной передачи строки
            self.key = self.key.encode("utf-8")
        self._fernet = Fernet(self.key)

    @classmethod
    def from_path(cls, path: Path, env_value: str | None = None) -> "SecretBox":
        """Собрать шифровальщик из настройки/файла секрета."""
        return cls(key=load_or_create_secret(path, env_value))

    def encrypt(self, plaintext: str | None) -> str | None:
        """Зашифровать значение; `None`/пусто → `None` (пароль «не задан»)."""
        if not plaintext:
            return None
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str | None) -> str | None:
        """Расшифровать значение; нечитаемый токен → `None` + предупреждение в лог."""
        if not token:
            return None
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            logger.warning("Не удалось расшифровать секрет: файл ключа заменён или повреждён")
            return None

    def reencrypt(self, token: str | None) -> str | None:
        """Перешифровать значение текущим ключом (процедура ротации ключа, RUNBOOK)."""
        return self.encrypt(self.decrypt(token))


class SessionSigner:
    """Подписанная cookie администратора (§4.2, §8.1)."""

    def __init__(self, secret_key: bytes | str, *, salt: str = COOKIE_SALT) -> None:
        if isinstance(secret_key, str):
            secret_key = secret_key.encode("utf-8")
        self._serializer = URLSafeTimedSerializer(secret_key=secret_key, salt=salt)

    def dump(self, payload: dict[str, Any]) -> str:
        """Подписать данные сессии."""
        return self._serializer.dumps(payload)

    def load(self, token: str | None, *, max_age: int) -> dict[str, Any] | None:
        """Проверить подпись и срок жизни; на подделку/просрочку — `None`."""
        if not token:
            return None
        try:
            payload = self._serializer.loads(token, max_age=max_age)
        except (BadSignature, SignatureExpired):
            return None
        return payload if isinstance(payload, dict) else None

