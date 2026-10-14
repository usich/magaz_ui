"""API администраторов `/api/v1/admin/admins*` (§5.2.2, §11.6) — Д-8, состав Э6.

Что закрывает модуль (Д-8): управление администраторами дашборда из `/settings`:

* `GET /admins` — список (действующие первыми), вместе с признаком блокировки;
* `POST /admins` — создание: логин + пароль, проверка длины и уникальности;
* `PUT /admins/{id}` — смена пароля и/или блокировка (`is_active=false`);
* `DELETE /admins/{id}` — удаление учётной записи.

Правила, общие для блокировки и удаления, живут в `admin_service`: себя и
последнего действующего администратора трогать нельзя (иначе вход в дашборд
теряется безвозвратно). Хеш пароля не покидает БД: в ответах только `AdminOut`,
в аудите — факты без значений (I-7).

Роутер защищён `require_active_admin`: «новый эндпоинт по умолчанию защищён»,
как в `server/api/stores.py`, `devices.py` и `notifications.py`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from server.core.context import AppContext
from server.core.dependencies import get_client_ip, get_context, get_db, require_active_admin
from server.db.models import AdminUser
from server.services import admin_service, audit_service

logger = logging.getLogger(__name__)

__all__ = ["AdminOut", "router"]

router = APIRouter(
    prefix="/admins",
    tags=["admin-admins"],
    dependencies=[Depends(require_active_admin)],
)

#: 422 без deprecation-предупреждения Starlette — как в `admin.py` и `notifications.py`
HTTP_422 = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)


class AdminOut(BaseModel):
    """Администратор в API (§4.2): хеш пароля наружу не уходит никогда."""

    id: int
    username: str
    is_active: bool
    last_login_at: datetime | None = None

    @classmethod
    def from_model(cls, admin: AdminUser) -> "AdminOut":
        """Собрать ответ из модели: ровно те поля, что нужны UI."""
        return cls(
            id=admin.id,
            username=admin.username,
            is_active=admin.is_active,
            last_login_at=admin.last_login_at,
        )


class AdminCreateIn(BaseModel):
    """Тело `POST /admins` (Д-8)."""

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1)


class AdminUpdateIn(BaseModel):
    """Тело `PUT /admins/{id}`: пароль и/или блокировка.

    Оба поля необязательны, но хотя бы одно должно прийти — «пустой» PUT
    отвечает `422`, чтобы UI не показывал успех без изменений.
    """

    password: str | None = None
    is_active: bool | None = None


def _min_password_length(context: AppContext) -> int:
    """Минимальная длина пароля администратора (`server/config.py`)."""
    return context.settings.admin_password_min_length


def _get_or_404(session: Session, admin_id: int) -> AdminUser:
    """Администратор по id или `404` (удалённый тоже сюда попадает)."""
    admin = admin_service.get_by_id(session, admin_id)
    if admin is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Администратор не найден"
        )
    return admin


# --- Список и создание ------------------------------------------------------


@router.get("", response_model=list[AdminOut], summary="Администраторы дашборда")
def list_admins(session: Annotated[Session, Depends(get_db)]) -> list[AdminOut]:
    """Список администраторов (§11.6): действующие первыми, затем по алфавиту."""
    return [AdminOut.from_model(admin) for admin in admin_service.list_admins(session)]


@router.post(
    "",
    response_model=AdminOut,
    status_code=status.HTTP_201_CREATED,
    summary="Создать администратора",
)
def create_admin(
    payload: AdminCreateIn,
    request: Request,
    context: Annotated[AppContext, Depends(get_context)],
    actor: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> AdminOut:
    """Создать администратора: `409` при занятом логине, `422` при слабом пароле."""
    try:
        created = admin_service.create_admin(
            session,
            username=payload.username,
            password=payload.password,
            min_password_length=_min_password_length(context),
        )
    except admin_service.AdminExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:  # пустой логин, короткий пароль
        raise HTTPException(status_code=HTTP_422, detail=str(exc)) from exc

    audit_service.record(
        session,
        action="admin_create",
        actor=actor.username,
        entity="admin_user",
        entity_id=created.id,
        details={"username": created.username},
        ip_address=get_client_ip(request),
    )
    logger.info("Администратор %s создан администратором %s", created.username, actor.username)
    return AdminOut.from_model(created)


# --- Изменение и удаление ---------------------------------------------------


@router.put("/{admin_id}", response_model=AdminOut, summary="Изменить администратора")
def update_admin(
    admin_id: int,
    payload: AdminUpdateIn,
    request: Request,
    context: Annotated[AppContext, Depends(get_context)],
    actor: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> AdminOut:
    """Сменить пароль и/или заблокировать: `422` слабый пароль, `403`/`409` запреты.

    Пароль проверяется первым: если администратор прислал и новый пароль, и
    блокировку, то сначала валидируется пароль, и только потом меняется доступ.
    """
    target = _get_or_404(session, admin_id)
    if payload.password is None and payload.is_active is None:
        raise HTTPException(
            status_code=HTTP_422,
            detail="Нечего изменять: передайте password и/или is_active",
        )

    if payload.password is not None:
        try:
            admin_service.set_password(
                session,
                target,
                password=payload.password,
                min_password_length=_min_password_length(context),
            )
        except admin_service.WeakPasswordError as exc:
            raise HTTPException(status_code=HTTP_422, detail=str(exc)) from exc
        audit_service.record(
            session,
            action="admin_password_change",
            actor=actor.username,
            entity="admin_user",
            entity_id=target.id,
            details={"username": target.username},
            ip_address=get_client_ip(request),
        )

    if payload.is_active is not None and payload.is_active != target.is_active:
        try:
            admin_service.set_active(
                session, target, is_active=payload.is_active, actor_id=actor.id
            )
        except admin_service.AdminSelfError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except admin_service.LastActiveAdminError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        audit_service.record(
            session,
            action="admin_unblock" if payload.is_active else "admin_block",
            actor=actor.username,
            entity="admin_user",
            entity_id=target.id,
            details={"username": target.username},
            ip_address=get_client_ip(request),
        )
        logger.info(
            "Доступ администратора %s %s (изменил %s)",
            target.username,
            "возвращён" if payload.is_active else "заблокирован",
            actor.username,
        )

    return AdminOut.from_model(target)


@router.delete("/{admin_id}", summary="Удалить администратора")
def delete_admin(
    admin_id: int,
    request: Request,
    actor: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> dict[str, bool]:
    """Удалить администратора: себя и последнего действующего — нельзя (Д-8)."""
    target = _get_or_404(session, admin_id)
    username = target.username
    try:
        admin_service.delete_admin(session, target, actor_id=actor.id)
    except admin_service.AdminSelfError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except admin_service.LastActiveAdminError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    audit_service.record(
        session,
        action="admin_delete",
        actor=actor.username,
        entity="admin_user",
        entity_id=admin_id,
        details={"username": username},
        ip_address=get_client_ip(request),
    )
    logger.info("Администратор %s удалён администратором %s", username, actor.username)
    return {"ok": True}
