"""Локальные базы 1С (Д-20) — сервисный слой.

Локальная база — это база 1С на машине дашборда (в отличие от магазина с
агентом). Обновление пойдёт напрямую, без задач агенту; здесь — реквизиты
подключения (версия 1С, кластер, имя БД, логин/пароль 1С), путь к базе для
будущего обновления, имя расширения и поле группировки `group`.

Пароль 1С шифруется Fernet (§8.3) и наружу уходит только признак
`password_set` — так же, как у магазина (П-1).
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.core.security import SecretBox
from server.db.models import LocalBase

logger = logging.getLogger(__name__)

__all__ = [
    "LocalBaseNotFoundError",
    "LocalBaseValidationError",
    "create_local_base",
    "deactivate_local_base",
    "get_local_base",
    "list_local_bases",
    "set_credentials",
    "update_local_base",
]


class LocalBaseValidationError(ValueError):
    """Данные локальной базы не проходят проверку."""


class LocalBaseNotFoundError(LookupError):
    """Локальная база с указанным id не найдена."""


def _require_text(value: str | None, *, field: str) -> str:
    """Непустое текстовое поле (иначе — понятная ошибка валидации)."""
    raw = (value or "").strip()
    if not raw:
        raise LocalBaseValidationError(f"Поле «{field}» не может быть пустым")
    return raw


def _require_unique_name(session: Session, name: str, *, exclude_id: int | None = None) -> None:
    """Имя локальной базы уникально среди активных (casefold, как у магазинов)."""
    wanted = name.strip().casefold()
    stmt = select(LocalBase.id).where(LocalBase.is_active.is_(True))
    if exclude_id is not None:
        stmt = stmt.where(LocalBase.id != exclude_id)
    for (row_id,) in session.execute(stmt):
        existing = session.get(LocalBase, row_id)
        if existing and existing.name.strip().casefold() == wanted:
            raise LocalBaseValidationError(f"База с названием «{name.strip()}» уже существует")


def _flush_or_conflict(session: Session) -> None:
    """Сохранить изменения; дубликат из БД превращаем в понятную ошибку."""
    try:
        session.flush()
    except IntegrityError as exc:  # pragma: no cover - страховка поверх Python-проверки
        session.rollback()
        raise LocalBaseValidationError("База с такими данными уже существует") from exc


def create_local_base(
    session: Session,
    *,
    name: str,
    version_1c: str,
    cluster_name: str,
    database_name: str,
    user_name_1c: str,
    base_path: str | None = None,
    extension_name: str | None = None,
    group: str = "",
    password: str | None = None,
    comment: str | None = None,
    secret_box: SecretBox | None = None,
) -> LocalBase:
    """Создать локальную базу; пароль шифруется, если передан."""
    normalized_name = _require_text(name, field="Название базы")
    _require_unique_name(session, normalized_name)
    row = LocalBase(
        name=normalized_name,
        version_1c=_require_text(version_1c, field="Версия 1С"),
        cluster_name=_require_text(cluster_name, field="Кластер (сервер 1С)"),
        database_name=_require_text(database_name, field="Имя базы 1С"),
        user_name_1c=_require_text(user_name_1c, field="Пользователь 1С"),
        base_path=(base_path or "").strip() or None,
        extension_name=(extension_name or "").strip() or None,
        group=(group or "").strip(),
        comment=(comment or "").strip() or None,
        credentials_version=1,
    )
    if password:
        if secret_box is None:  # pragma: no cover - защита от неверного вызова
            raise LocalBaseValidationError("Не передан шифровальщик для пароля 1С")
        row.password_enc = secret_box.encrypt(password)
    session.add(row)
    _flush_or_conflict(session)
    logger.info("Локальная база id=%s «%s» создана (группа «%s»)", row.id, row.name, row.group)
    return row


def get_local_base(session: Session, base_id: int) -> LocalBase:
    """Локальная база по id; отсутствие — `LocalBaseNotFoundError`."""
    row = session.get(LocalBase, base_id)
    if row is None:
        raise LocalBaseNotFoundError(f"Локальная база с id={base_id} не найдена")
    return row

def update_local_base(
    session: Session,
    row: LocalBase,
    *,
    name: str | None = None,
    version_1c: str | None = None,
    cluster_name: str | None = None,
    database_name: str | None = None,
    user_name_1c: str | None = None,
    base_path: str | None = None,
    extension_name: str | None = None,
    group: str | None = None,
    comment: str | None = None,
    is_active: bool | None = None,
) -> LocalBase:
    """Изменить реквизиты локальной базы (передаются только изменяемые поля)."""
    if name is not None:
        normalized_name = _require_text(name, field="Название базы")
        _require_unique_name(session, normalized_name, exclude_id=row.id)
        row.name = normalized_name
    if version_1c is not None:
        row.version_1c = _require_text(version_1c, field="Версия 1С")
    if cluster_name is not None:
        row.cluster_name = _require_text(cluster_name, field="Кластер (сервер 1С)")
    if database_name is not None:
        row.database_name = _require_text(database_name, field="Имя базы 1С")
    if user_name_1c is not None:
        normalized_user = _require_text(user_name_1c, field="Пользователь 1С")
        if normalized_user != row.user_name_1c:
            row.user_name_1c = normalized_user
            row.credentials_version += 1
    if base_path is not None:
        row.base_path = (base_path or "").strip() or None
    if extension_name is not None:
        row.extension_name = (extension_name or "").strip() or None
    if group is not None:
        row.group = (group or "").strip()
    if comment is not None:
        row.comment = (comment or "").strip() or None
    if is_active is not None:
        if is_active and not row.is_active:
            _require_unique_name(session, row.name, exclude_id=row.id)
        row.is_active = bool(is_active)

    _flush_or_conflict(session)
    return row


def set_credentials(
    session: Session,
    row: LocalBase,
    *,
    user_name_1c: str | None = None,
    password: str | None = None,
    clear_password: bool = False,
    secret_box: SecretBox | None = None,
) -> LocalBase:
    """Задать/заменить/очистить учётные данные 1С (П-1, §8.3)."""
    if user_name_1c is not None:
        row.user_name_1c = _require_text(user_name_1c, field="Пользователь 1С")

    if clear_password:
        row.password_enc = None
    elif password:
        if secret_box is None:  # pragma: no cover - защита от неверного вызова
            raise LocalBaseValidationError("Не передан шифровальщик для пароля 1С")
        row.password_enc = secret_box.encrypt(password)

    row.credentials_version += 1
    session.flush()
    logger.info(
        "Учётные данные 1С локальной базы id=%s обновлены (версия %s, пароль %s)",
        row.id,
        row.credentials_version,
        "задан" if row.password_set else "не задан",
    )
    return row


def deactivate_local_base(session: Session, row: LocalBase) -> LocalBase:
    """Мягкое удаление (§5.2.2): `is_active=false` — база уходит из списка."""
    row.is_active = False
    session.flush()
    logger.info("Локальная база id=%s переведена в неактивные", row.id)
    return row


def list_local_bases(
    session: Session,
    *,
    search: str | None = None,
    group: str | None = None,
    include_inactive: bool = False,
) -> list[LocalBase]:
    """Список локальных баз; фильтры по поиску и группе (§Д-20)."""
    stmt = select(LocalBase)
    if not include_inactive:
        stmt = stmt.where(LocalBase.is_active.is_(True))
    if group:
        stmt = stmt.where(LocalBase.group == group.strip())
    rows = list(session.scalars(stmt.order_by(LocalBase.group, LocalBase.name)).all())

    query = (search or "").strip().casefold()
    if query:
        rows = [
            row
            for row in rows
            if query
            in " ".join(
                (
                    row.name or "",
                    row.database_name or "",
                    row.cluster_name or "",
                    row.group or "",
                )
            ).casefold()
        ]
    return rows


