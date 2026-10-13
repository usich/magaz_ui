"""Администраторы (§4.2): создание (в т.ч. CLI-командой) и проверка входа."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from server.core.security import hash_password, verify_password
from server.db.models import AdminUser
from shared.timeutil import utcnow

__all__ = ["AdminExistsError", "WeakPasswordError", "authenticate", "create_admin", "get_by_username"]

#: Минимальная длина пароля администратора (настраивается в `server/config.py`)
DEFAULT_MIN_PASSWORD_LENGTH = 8


class AdminExistsError(ValueError):
    """Администратор с таким логином уже существует."""


class WeakPasswordError(ValueError):
    """Пароль короче минимальной длины."""


def get_by_username(session: Session, username: str) -> AdminUser | None:
    """Найти администратора по логину (регистр не учитываем)."""
    normalized = (username or "").strip().lower()
    if not normalized:
        return None
    stmt = select(AdminUser).where(func.lower(AdminUser.username) == normalized)
    return session.scalars(stmt).first()


def create_admin(
    session: Session,
    *,
    username: str,
    password: str,
    min_password_length: int = DEFAULT_MIN_PASSWORD_LENGTH,
) -> AdminUser:
    """Создать администратора; проверяет длину пароля и уникальность логина."""
    normalized = (username or "").strip()
    if not normalized:
        raise ValueError("Логин не может быть пустым")
    if len(password or "") < min_password_length:
        raise WeakPasswordError(
            f"Пароль короче {min_password_length} символов — это небезопасно"
        )
    if get_by_username(session, normalized) is not None:
        raise AdminExistsError(f"Администратор {normalized!r} уже существует")

    admin = AdminUser(username=normalized, password_hash=hash_password(password), is_active=True)
    session.add(admin)
    session.flush()
    return admin


def authenticate(session: Session, *, username: str, password: str) -> AdminUser | None:
    """Проверить логин/пароль; при успехе обновляет `last_login_at`.

    На несуществующего пользователя тратится столько же времени (фиктивная
    проверка хеша), иначе логин перебирается по времени ответа.
    """
    admin = get_by_username(session, username)
    if admin is None:
        verify_password(password or "", hash_password("dummy-password-for-timing"))
        return None
    if not admin.is_active or not verify_password(password or "", admin.password_hash):
        return None

    admin.last_login_at = utcnow()
    session.flush()
    return admin
