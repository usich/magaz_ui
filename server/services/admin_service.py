"""Администраторы (§4.2): создание (в т.ч. CLI-командой), проверка входа.

Э6 (Д-8) добавляет управление администраторами из дашборда: список, смена
пароля, блокировка и удаление. Два запрета живут здесь, а не в API, потому что
это правило целостности доступа, а не оформление ответа:

* нельзя заблокировать/удалить **себя** — иначе администратор закрывает себе
  доступ к дашборду одним действием;
* нельзя заблокировать/удалить **последнего действующего** администратора —
  иначе в системе не остаётся никого, кто может войти.

Хеш пароля наружу не отдаёт ни один метод: возвращается модель `AdminUser`,
а сериализацией занимается API (`server/api/admins.py`).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from server.core.security import hash_password, verify_password
from server.db.models import AdminUser
from shared.timeutil import utcnow

__all__ = [
    "AdminExistsError",
    "AdminNotFoundError",
    "AdminSelfError",
    "LastActiveAdminError",
    "WeakPasswordError",
    "authenticate",
    "count_active",
    "create_admin",
    "delete_admin",
    "get_by_id",
    "get_by_username",
    "list_admins",
    "set_active",
    "set_password",
]

#: Минимальная длина пароля администратора (настраивается в `server/config.py`)
DEFAULT_MIN_PASSWORD_LENGTH = 8


class AdminExistsError(ValueError):
    """Администратор с таким логином уже существует."""


class WeakPasswordError(ValueError):
    """Пароль короче минимальной длины."""


class AdminNotFoundError(LookupError):
    """Администратора с таким id нет (или он уже удалён)."""


class AdminSelfError(ValueError):
    """Попытка заблокировать или удалить собственную учётную запись."""


class LastActiveAdminError(ValueError):
    """Попытка заблокировать или удалить последнего действующего администратора."""



def get_by_username(session: Session, username: str) -> AdminUser | None:
    """Найти администратора по логину (регистр не учитываем)."""
    normalized = (username or "").strip().lower()
    if not normalized:
        return None
    stmt = select(AdminUser).where(func.lower(AdminUser.username) == normalized)
    return session.scalars(stmt).first()


def get_by_id(session: Session, admin_id: int) -> AdminUser | None:
    """Найти администратора по id (404-семантику добавляет слой API)."""
    return session.get(AdminUser, admin_id)


def list_admins(session: Session) -> list[AdminUser]:
    """Все администраторы: сначала действующие, затем по алфавиту логина.

    Заблокированные аккаунты из списка не исчезают — иначе в UI пропадал бы
    ответ на вопрос «кто заблокирован и когда».
    """
    stmt = select(AdminUser).order_by(
        AdminUser.is_active.desc(), func.lower(AdminUser.username)
    )
    return list(session.scalars(stmt).all())


def count_active(session: Session) -> int:
    """Сколько администраторов могут войти прямо сейчас."""
    stmt = select(func.count()).select_from(AdminUser).where(AdminUser.is_active.is_(True))
    return int(session.scalar(stmt) or 0)


def _ensure_not_last_active(session: Session, admin: AdminUser, actor_id: int | None) -> None:
    """Общий запрет для блокировки и удаления: себя и последнего действующего."""
    if actor_id is not None and admin.id == actor_id:
        raise AdminSelfError(
            "Нельзя заблокировать или удалить свою учётную запись — это закрывает вход в дашборд"
        )
    if admin.is_active and count_active(session) <= 1:
        raise LastActiveAdminError(
            "Это последний действующий администратор: сначала создайте или разблокируйте другого"
        )


def set_password(
    session: Session,
    admin: AdminUser,
    *,
    password: str,
    min_password_length: int = DEFAULT_MIN_PASSWORD_LENGTH,
) -> AdminUser:
    """Сменить пароль администратора (уже выданные cookie продолжают жить, §8.2)."""
    if len(password or "") < min_password_length:
        raise WeakPasswordError(
            f"Пароль короче {min_password_length} символов — это небезопасно"
        )
    admin.password_hash = hash_password(password)
    session.flush()
    return admin


def set_active(
    session: Session,
    admin: AdminUser,
    *,
    is_active: bool,
    actor_id: int | None = None,
) -> AdminUser:
    """Заблокировать (`is_active=false`) или вернуть доступ администратору (Д-8)."""
    if not is_active:
        _ensure_not_last_active(session, admin, actor_id)
    admin.is_active = is_active
    session.flush()
    return admin


def delete_admin(session: Session, admin: AdminUser, *, actor_id: int | None = None) -> None:
    """Удалить администратора (Д-8).

    История остаётся: в `audit_log.actor` лежит строка логина, внешнего ключа на
    `admin_users` нет, поэтому записи аудита удалённого аккаунта не рассыпаются.
    """
    _ensure_not_last_active(session, admin, actor_id)
    session.delete(admin)
    session.flush()


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
