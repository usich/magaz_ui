"""API локальных баз 1С `/api/v1/admin/local-bases/*` (Д-20) — CRUD и учётные данные.

Локальная база — база 1С на машине дашборда: обновление пойдёт напрямую, без
задач агенту. Реквизиты как у магазина (версия 1С, кластер, имя БД, логин/пароль
1С), плюс `base_path` (путь к базе), `extension_name` (расширение) и `group`
(группировка в админке). Пароль 1С наружу не уходит: только признак `password_set`.

Роутер защищён `require_active_admin`, как и остальные админские (§5.2.2).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.core.context import AppContext
from server.core.dependencies import get_client_ip, get_context, get_db, require_active_admin
from server.db.models import AdminUser, LocalBase
from server.services import audit_service, backup_service, local_base_service
from shared.timeutil import to_iso_z

logger = logging.getLogger(__name__)

__all__ = ["router"]

router = APIRouter(
    prefix="/local-bases",
    tags=["admin-local-bases"],
    dependencies=[Depends(require_active_admin)],
)


# --- Схемы ответов/запросов -------------------------------------------------


class LocalBaseOut(BaseModel):
    """Локальная база в API (Д-20). Пароля 1С здесь нет — только `password_set`."""

    id: int
    name: str
    version_1c: str
    cluster_name: str
    database_name: str
    user_name_1c: str
    password_set: bool
    credentials_version: int
    base_path: str | None = None
    extension_name: str | None = None
    group: str = ""
    is_active: bool
    comment: str | None = None
    # Д-21: проверка бэкапа
    backup_check_enabled: bool = False
    backup_path: str | None = None
    backup_host: str | None = None
    backup_use_auth: bool = False
    backup_user: str | None = None
    backup_password_set: bool = False
    backup_check_yesterday: bool = False
    backup_status: str = ""
    backup_error: str | None = None
    backup_checked_at: str | None = None
    backup_checked_date: str | None = None
    backup_tries: int = 0
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_model(cls, base: LocalBase) -> "LocalBaseOut":
        return cls(
            id=base.id,
            name=base.name,
            version_1c=base.version_1c,
            cluster_name=base.cluster_name,
            database_name=base.database_name,
            user_name_1c=base.user_name_1c,
            password_set=base.password_set,
            credentials_version=base.credentials_version,
            base_path=base.base_path,
            extension_name=base.extension_name,
            group=base.group,
            is_active=base.is_active,
            comment=base.comment,
            backup_check_enabled=base.backup_check_enabled,
            backup_path=base.backup_path,
            backup_host=base.backup_host,
            backup_use_auth=base.backup_use_auth,
            backup_user=base.backup_user,
            backup_password_set=base.backup_password_set,
            backup_check_yesterday=base.backup_check_yesterday,
            backup_status=base.backup_status or "",
            backup_error=base.backup_error,
            backup_checked_at=to_iso_z(base.backup_checked_at),
            backup_checked_date=base.backup_checked_date,
            backup_tries=base.backup_tries,
            created_at=to_iso_z(base.created_at),
            updated_at=to_iso_z(base.updated_at),
        )


class LocalBaseCreateRequest(BaseModel):
    """Тело `POST /local-bases` — реквизиты + опциональный пароль 1С (П-1)."""

    name: str
    version_1c: str
    cluster_name: str
    database_name: str
    user_name_1c: str
    base_path: str | None = None
    extension_name: str | None = None
    group: str = ""
    password: str | None = None
    comment: str | None = None


class LocalBaseUpdateRequest(BaseModel):
    """Тело `PUT /local-bases/{id}`: передаются только изменяемые поля."""

    name: str | None = None
    version_1c: str | None = None
    cluster_name: str | None = None
    database_name: str | None = None
    user_name_1c: str | None = None
    base_path: str | None = None
    extension_name: str | None = None
    group: str | None = None
    comment: str | None = None
    is_active: bool | None = None
    # Д-21: настройки проверки бэкапа (передаются только изменяемые поля)
    backup_check_enabled: bool | None = None
    backup_path: str | None = None
    backup_host: str | None = None
    backup_use_auth: bool | None = None
    backup_user: str | None = None
    backup_password: str | None = None
    backup_clear_password: bool = False
    backup_check_yesterday: bool | None = None


class CredentialsRequest(BaseModel):
    """Тело `PUT /local-bases/{id}/credentials` (П-1, §8.3)."""

    user_name_1c: str | None = None
    password: str | None = None
    clear_password: bool = False


def _get_or_404(session: Session, base_id: int) -> LocalBase:
    """Локальная база по id; отсутствие — `404`."""
    try:
        return local_base_service.get_local_base(session, base_id)
    except local_base_service.LocalBaseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

# --- CRUD локальных баз -----------------------------------------------------


@router.get("", response_model=list[LocalBaseOut], summary="Список локальных баз")
def list_local_bases_endpoint(
    session: Annotated[Session, Depends(get_db)],
    search: Annotated[str | None, Query(max_length=200)] = None,
    group: Annotated[str | None, Query(max_length=100)] = None,
    include_inactive: bool = False,
) -> list[LocalBaseOut]:
    """Список с фильтрами по поиску и группе (Д-20)."""
    rows = local_base_service.list_local_bases(
        session,
        search=search,
        group=group,
        include_inactive=include_inactive,
    )
    return [LocalBaseOut.from_model(row) for row in rows]


@router.post("", response_model=LocalBaseOut, summary="Создать локальную базу")
def create_local_base_endpoint(
    payload: LocalBaseCreateRequest,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    context: Annotated[AppContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db)],
) -> LocalBaseOut:
    """Создать локальную базу; пароль 1С шифруется на сервере (П-1)."""
    try:
        row = local_base_service.create_local_base(
            session,
            name=payload.name,
            version_1c=payload.version_1c,
            cluster_name=payload.cluster_name,
            database_name=payload.database_name,
            user_name_1c=payload.user_name_1c,
            base_path=payload.base_path,
            extension_name=payload.extension_name,
            group=payload.group,
            password=payload.password,
            comment=payload.comment,
            secret_box=context.secret_box,
        )
    except local_base_service.LocalBaseValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    audit_service.record(
        session,
        action="local_base_create",
        actor=admin.username,
        entity="local_base",
        entity_id=row.id,
        details={"name": row.name, "group": row.group},
        ip_address=get_client_ip(request),
    )
    return LocalBaseOut.from_model(row)


@router.get("/{base_id}", response_model=LocalBaseOut, summary="Карточка локальной базы")
def get_local_base_endpoint(
    base_id: int,
    session: Annotated[Session, Depends(get_db)],
) -> LocalBaseOut:
    """Реквизиты локальной базы."""
    return LocalBaseOut.from_model(_get_or_404(session, base_id))


@router.put("/{base_id}", response_model=LocalBaseOut, summary="Изменить локальную базу")
def update_local_base_endpoint(
    base_id: int,
    payload: LocalBaseUpdateRequest,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    context: Annotated[AppContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db)],
) -> LocalBaseOut:
    """Изменить реквизиты; смена логина 1С поднимает `credentials_version`."""
    row = _get_or_404(session, base_id)

    backup_keys = {
        "backup_check_enabled",
        "backup_path",
        "backup_host",
        "backup_use_auth",
        "backup_user",
        "backup_password",
        "backup_clear_password",
        "backup_check_yesterday",
    }
    changed = payload.model_dump(exclude_unset=True, exclude_none=True)
    ordinary = {k: v for k, v in changed.items() if k not in backup_keys}

    try:
        local_base_service.update_local_base(session, row, **ordinary)
    except local_base_service.LocalBaseValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    # Д-21: настройки проверки бэкапа — отдельным хелпером (пароль шифруется)
    if any(k in changed for k in backup_keys):
        backup_service.apply_backup_settings(
            row,
            enabled=changed.get("backup_check_enabled"),
            path=changed.get("backup_path"),
            host=changed.get("backup_host"),
            use_auth=changed.get("backup_use_auth"),
            user=changed.get("backup_user"),
            password=changed.get("backup_password"),
            clear_password=payload.backup_clear_password,
            check_yesterday=changed.get("backup_check_yesterday"),
            secret_box=context.secret_box,
        )
        session.flush()

    audit_service.record(
        session,
        action="local_base_update",
        actor=admin.username,
        entity="local_base",
        entity_id=row.id,
        details={"fields": sorted(changed.keys())},
        ip_address=get_client_ip(request),
    )
    return LocalBaseOut.from_model(row)


@router.delete("/{base_id}", response_model=LocalBaseOut, summary="Отключить локальную базу")
def deactivate_local_base_endpoint(
    base_id: int,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> LocalBaseOut:
    """Мягкое удаление (§5.2.2): `is_active=false` — база уходит из списка."""
    row = _get_or_404(session, base_id)
    local_base_service.deactivate_local_base(session, row)
    audit_service.record(
        session,
        action="local_base_deactivate",
        actor=admin.username,
        entity="local_base",
        entity_id=row.id,
        details={"name": row.name},
        ip_address=get_client_ip(request),
    )
    return LocalBaseOut.from_model(row)


@router.put(
    "/{base_id}/credentials",
    response_model=LocalBaseOut,
    summary="Учётные данные 1С локальной базы (П-1)",
)
def set_credentials_endpoint(
    base_id: int,
    payload: CredentialsRequest,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    context: Annotated[AppContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db)],
) -> LocalBaseOut:
    """Задать/заменить/очистить пароль 1С — значение не логируется и не возвращается."""
    row = _get_or_404(session, base_id)
    local_base_service.set_credentials(
        session,
        row,
        user_name_1c=payload.user_name_1c,
        password=payload.password,
        clear_password=payload.clear_password,
        secret_box=context.secret_box,
    )
    audit_service.record(
        session,
        action="local_base_credentials_update",
        actor=admin.username,
        entity="local_base",
        entity_id=row.id,
        details={
            "credentials_version": row.credentials_version,
            "password_set": row.password_set,
            "cleared": bool(payload.clear_password),
        },
        ip_address=get_client_ip(request),
    )
    return LocalBaseOut.from_model(row)

