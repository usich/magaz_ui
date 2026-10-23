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
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from server.core.context import AppContext
from server.core.dependencies import get_client_ip, get_context, get_db, require_active_admin
from server.db.models import AdminUser, LocalBase
from server.services import audit_service, backup_service, local_base_service, command_service
from shared.enums import CommandKind, CommandStatus
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



# --- Обновление локальной базы (Д-24) ----------------------------------------

from shared.paths import split_merge_file_name


def _build_local_update_params(
    payload: LocalUpdateRunRequest,
) -> dict[str, object]:
    """Собрать параметры задачи обновления локальной базы из тела запроса (Д-24).

    Тип обновления (`is_extension`) по умолчанию определяется по имени файла:
    `.cfe` → расширение, `.cf` → основная конфигурация (Д-16).
    """
    params: dict[str, object] = {
        "file_name": payload.file_name,
        "is_fast_update": payload.is_fast_update,
        "is_extension": payload.is_extension,
    }
    if payload.extension_name:
        params["extension_name"] = payload.extension_name
    else:
        _, suffix = split_merge_file_name(payload.file_name)
        params["is_extension"] = bool(payload.is_extension) if payload.is_extension is not None else (suffix == ".cfe")
    return params


class LocalUpdateRunRequest(BaseModel):
    """Тело `POST /local-bases/{id}/run-update`: файл и режим операции.

    `file_name` — имя файла обновления без расширения или с явным `.cf`/`.cfe`
    (как у магазинов, Д-16); `is_fast_update` — быстрое обновление без завершения
    сеансов; `is_extension` — явный тип (расширение/основная), если нужен.
    """

    file_name: str = "update"
    is_fast_update: bool = False
    is_extension: bool | None = None
    extension_name: str | None = None


class LocalUpdateJobOut(BaseModel):
    """Поставленная задача обновления локальной базы (Д-24). Секретов нет."""

    id: int
    local_base_id: int
    local_base_name: str | None = None
    kind: str
    status: str
    created_at: str | None = None


class LocalUpdateRunOut(BaseModel):
    """Ответ `POST /local-bases/{id}/run-update`: задача в очереди (Д-24)."""

    message: str
    job: LocalUpdateJobOut


class LocalBulkUpdateJobOut(BaseModel):
    """Строка результата массового обновления по одной базе (Д-25)."""

    local_base_id: int
    local_base_name: str = ""
    job_id: int | None = None
    status: str = CommandStatus.QUEUED
    detail: str | None = None


class LocalBulkUpdateOut(BaseModel):
    """Ответ `POST /local-bases/bulk/update` (Д-25): по строке на базу."""

    message: str
    total: int = 0
    jobs: list[LocalBulkUpdateJobOut] = Field(default_factory=list)


@router.post(
    "/{base_id}/run-update",
    response_model=LocalUpdateRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Обновить локальную базу 1С (Д-24)",
)
def run_update_local_base(
    base_id: int,
    payload: LocalUpdateRunRequest,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> LocalUpdateRunOut:
    """Поставить задачу обновления локальной базы в очередь (Д-24).

    Задачу (`run_update_db_local`) выполнит LocalUpdateMonitor напрямую на этой
    машине; ответ `202`, результат — в `/jobs` и журнале `/jobs/{id}`. Пароль 1С
    в параметры не попадает (I-7): подключение берётся из карточки базы.
    R12-запрет «одна локальная база — одно обновление» — под `dispatch_guard`.
    """
    row = _get_or_404(session, base_id)
    params = _build_local_update_params(payload)

    with command_service.dispatch_guard():
        busy = command_service.find_local_update_conflict(session, row.id)
        if busy is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"База «{row.name}» уже обновляется (задача #{busy.id}) — "
                    "дождитесь завершения операции"
                ),
            )
        job = command_service.create_local_command(
            session,
            local_base_id=row.id,
            kind=CommandKind.RUN_UPDATE_DB_LOCAL,
            params=params,
            created_by=admin.username,
        )
        session.commit()

    audit_service.record(
        session,
        action="local_base_run_update",
        actor=admin.username,
        entity="local_base",
        entity_id=row.id,
        details={
            "file_name": payload.file_name,
            "is_fast_update": payload.is_fast_update,
            "is_extension": params["is_extension"],
            "job_id": job.id,
        },
        ip_address=get_client_ip(request),
    )
    session.commit()
    logger.info(
        "Обновление локальной базы id=%s поставлено в очередь (задача #%s, %s)",
        row.id,
        job.id,
        "быстрое" if payload.is_fast_update else "полное",
    )
    return LocalUpdateRunOut(
        message=f"Обновление базы «{row.name}» поставлено в очередь",
        job=LocalUpdateJobOut(
            id=job.id,
            local_base_id=row.id,
            local_base_name=row.name,
            kind=job.kind,
            status=job.status,
            created_at=to_iso_z(job.created_at),
        ),
    )

class LocalBulkUpdateRequest(BaseModel):
    """Тело `POST /local-bases/bulk/update`: список баз + файл и режим (Д-25)."""

    local_base_ids: list[int] = Field(default_factory=list)
    file_name: str = "update"
    is_fast_update: bool = False
    is_extension: bool | None = None
    extension_name: str | None = None


@router.post(
    "/bulk/update",
    response_model=LocalBulkUpdateOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Массовое обновление локальных баз 1С (Д-25)",
)
def bulk_update_local_bases(
    payload: LocalBulkUpdateRequest,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> LocalBulkUpdateOut:
    """Поставить обновление пачке локальных баз (Д-25), как `bulk/actions` у магазинов.

    Каждой базе создаётся своя задача `run_update_db_local`; сколько из них
    выполняется параллельно — решает настройка `local_update_concurrency`
    (по умолчанию 5). База, которая уже обновляется (R12 «одна база — одно
    обновление»), пропускается со строкой `skipped` — остальные не блокируются.
    """
    ids = list(dict.fromkeys(payload.local_base_ids))
    if not ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Не выбрано ни одной локальной базы",
        )

    results: list[LocalBulkUpdateJobOut] = []
    with command_service.dispatch_guard():
        for base_id in ids:
            row = session.get(LocalBase, base_id)
            if row is None:
                results.append(
                    LocalBulkUpdateJobOut(
                        local_base_id=base_id,
                        local_base_name="",
                        status="skipped",
                        detail="Локальная база не найдена",
                    )
                )
                continue
            busy = command_service.find_local_update_conflict(session, base_id)
            if busy is not None:
                results.append(
                    LocalBulkUpdateJobOut(
                        local_base_id=base_id,
                        local_base_name=row.name,
                        status="skipped",
                        detail=f"уже обновляется (задача #{busy.id})",
                    )
                )
                continue
            params = _build_local_update_params(
                LocalUpdateRunRequest(
                    file_name=payload.file_name,
                    is_fast_update=payload.is_fast_update,
                    is_extension=payload.is_extension,
                    extension_name=payload.extension_name,
                )
            )
            job = command_service.create_local_command(
                session,
                local_base_id=base_id,
                kind=CommandKind.RUN_UPDATE_DB_LOCAL,
                params=params,
                created_by=admin.username,
            )
            results.append(
                LocalBulkUpdateJobOut(
                    local_base_id=base_id,
                    local_base_name=row.name,
                    job_id=job.id,
                    status=job.status,
                )
            )
        session.commit()

    created = sum(1 for item in results if item.job_id is not None)
    audit_service.record(
        session,
        action="local_base_bulk_update",
        actor=admin.username,
        entity="local_bases",
        entity_id=None,
        details={
            "local_base_ids": [item.local_base_id for item in results],
            "created": created,
            "file_name": payload.file_name,
            "is_fast_update": payload.is_fast_update,
        },
        ip_address=get_client_ip(request),
    )
    session.commit()
    logger.info("Массовое обновление локальных баз: создано %s из %s", created, len(results))
    return LocalBulkUpdateOut(
        message=f"Задачи поставлены: {created} из {len(results)} баз",
        total=len(results),
        jobs=results,
    )
