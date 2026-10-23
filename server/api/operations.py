"""API операций, задач и матрицы файлов (§5.2.2, §11.3, §11.4, §11.6) — состав Э4.

Модуль закрывает четыре узла контракта:

* `POST /stores/{id}/actions` — одиночное действие над магазином;
* `POST /bulk/actions` — массовое действие (по списку id, по кластеру, по всем);
* `GET /jobs`, `GET /jobs/{id}` — история операций и полный журнал задачи;
* `GET /files/matrix` — матрица «магазин × (cf/cfe/cfe/xml)» с размерами, датами
  и счётчиком «файл есть у N из M» (§11.3).

Ключевые правила, за которые отвечает модуль:

* **задача не выполняется в HTTP-запросе.** Действие создаёт строку `commands`
  в статусе `queued` и отвечает `202`; на кассу её ставит диспетчер (§3.3, I-8).
  Поэтому админская кнопка не «висит» на операции 1С длиной в час;
* **пароль 1С в задаче не появляется (П-1, I-7):** параметры чистятся, а
  реквизиты подключения сервер берёт из карточки магазина;
* **Q53** — отсутствие файла не блокирует запуск: возвращается `warning`,
  который UI показывает жёлтым, а жёсткая блокировка отдана явному флагу;
* **R12/FR-28 (П-5)** — запуск `run_merge` / `run_update_db` не создаётся, если
  нарушает запрет «одна серверная ИБ — одно обновление»: ответ `409`, задачи не
  создаются вовсе (в том числе в массовом запуске: либо все, либо ни одной).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from server.core.dependencies import get_client_ip, get_db, require_active_admin
from server.db.models import AdminUser, Command, FileCheck, JobLog, Store
from server.services import (
    audit_service,
    command_service,
    notification_service,
    settings_service,
)
from shared.enums import ActionKind, CommandKind, CommandStatus, TargetType
from shared.paths import UPDATE_FILE_SUFFIXES, is_extension_operation, split_merge_file_name
from shared.schemas import CheckFilesParams
from shared.timeutil import to_iso_z, utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "SUPPORTED_ACTIONS",
    "router",
    "resolve_target_stores",
]

router = APIRouter(tags=["admin-operations"], dependencies=[Depends(require_active_admin)])

#: 422 без deprecation-предупреждения Starlette (`HTTP_422_UNPROCESSABLE_CONTENT`
#: появился в новых релизах, старое имя помечено устаревшим) — как в `admin.py`
HTTP_422 = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)

#: Действия, которые сервер умеет ставить в очередь на этом этапе (§11.4).
#: Э5: `notify` — единственное действие, которое рождает ещё и сущность
#: `notifications`: см. `_create_notification_job()`.
SUPPORTED_ACTIONS: frozenset[str] = frozenset(
    {
        str(ActionKind.CHECK_FILES),
        str(ActionKind.DOWNLOAD_FILE),
        str(ActionKind.RUN_MERGE),
        str(ActionKind.RUN_UPDATE_DB),
        str(ActionKind.SEND_MESSAGE),
        str(ActionKind.NOTIFY),
        str(ActionKind.DELETE_FILE),
    }
)

#: Размер хвоста лога, который показывается прямо в списке задач (§11.6)
EXCERPT_PREVIEW_CHARS = 600

ACTION_KIND: dict[str, str] = {
    str(ActionKind.CHECK_FILES): str(CommandKind.CHECK_FILES),
    str(ActionKind.DOWNLOAD_FILE): str(CommandKind.DOWNLOAD_FILE),
    str(ActionKind.RUN_MERGE): str(CommandKind.RUN_MERGE),
    str(ActionKind.RUN_UPDATE_DB): str(CommandKind.RUN_UPDATE_DB),
    str(ActionKind.SEND_MESSAGE): str(CommandKind.SEND_MESSAGE),
    str(ActionKind.NOTIFY): str(CommandKind.NOTIFY),
    #: Э10: удаление файлов обновления на кассе
    str(ActionKind.DELETE_FILE): str(CommandKind.DELETE_FILE),
}


# --- Схемы запросов и ответов -----------------------------------------------


class ActionRequest(BaseModel):
    """Тело `POST /stores/{id}/actions` (§5.2.2)."""

    action: str
    params: dict[str, Any] = Field(default_factory=dict)


class BulkActionRequest(BaseModel):
    """Тело `POST /bulk/actions`: выбор магазинов + действие (§11.4)."""

    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    store_ids: list[int] = Field(default_factory=list)
    cluster: str | None = None
    all: bool = False


class JobOut(BaseModel):
    """Задача в списке `/jobs` (§4.3). Секретов здесь нет по построению.

    Д-24: локальная задача ссылается на `local_base_id`/`local_base_name`, а не
    на магазин — `store_id` тогда `None`. Ровно одно из полей заполнено.
    """

    id: int
    store_id: int | None = None
    store_name: str | None = None
    local_base_id: int | None = None
    local_base_name: str | None = None
    kind: str
    status: str
    params: dict[str, Any] | None = None
    attempts: int = 0
    last_error: str | None = None
    log_files: list[str] = Field(default_factory=list)
    created_by: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    last_message: str | None = None
    excerpt_preview: str | None = None

    @classmethod
    def from_model(cls, command: Command, *, last_message: str | None = None) -> "JobOut":
        excerpt = command.log_excerpt or ""
        return cls(
            id=command.id,
            store_id=command.store_id,
            store_name=getattr(command.store, "name", None),
            local_base_id=command.local_base_id,
            local_base_name=getattr(command.local_base, "name", None),
            kind=command.kind,
            status=command.status,
            params=command.params,
            attempts=int(command.attempts or 0),
            last_error=command.last_error,
            log_files=list(command.log_files or []),
            created_by=command.created_by,
            created_at=to_iso_z(command.created_at),
            started_at=to_iso_z(command.started_at),
            finished_at=to_iso_z(command.finished_at),
            last_message=last_message,
            excerpt_preview=excerpt[-EXCERPT_PREVIEW_CHARS:] if excerpt else None,
        )


class JobLogOut(BaseModel):
    """Строка журнала задачи (§4.4)."""

    id: int
    level: str
    message: str
    created_at: str | None = None

    @classmethod
    def from_model(cls, entry: JobLog) -> "JobLogOut":
        return cls(
            id=entry.id,
            level=entry.level,
            message=entry.message,
            created_at=to_iso_z(entry.created_at),
        )


class JobDetailOut(JobOut):
    """Задача с полным журналом (`GET /jobs/{id}`, §11.6)."""

    logs: list[JobLogOut] = Field(default_factory=list)


class JobsPageOut(BaseModel):
    """Страница истории задач: элементы, счётчики по статусам, всего (§11.6)."""

    items: list[JobOut] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    total: int = 0
    active: int = 0


class JobDeleteOut(BaseModel):
    """Ответ `DELETE /jobs/{id}`: что удалено (§11.6, Э10)."""

    job_id: int
    deleted: bool
    message: str


class JobsBulkDeleteRequest(BaseModel):
    """Тело `POST /jobs/bulk-delete`: список id выбранных задач (§11.6, Э10)."""

    ids: list[int] = Field(default_factory=list)


class JobsBulkDeleteOut(BaseModel):
    """Ответ `POST /jobs/bulk-delete`: сколько задач удалено (§11.6, Э10)."""

    deleted: int
    total: int
    message: str


class FileMatrixRow(BaseModel):
    """Строка матрицы `/files`: магазин и его последняя проверка (§11.3)."""

    store_id: int
    store_name: str
    ip_address: str
    cluster_name: str
    is_active: bool
    is_online: bool
    filename: str
    checked_at: str | None = None
    cf_exists: bool = False
    cfe_exists: bool = False
    xml_exists: bool = False
    cf_size: int | None = None
    cfe_size: int | None = None
    xml_size: int | None = None
    cf_mtime: str | None = None
    cfe_mtime: str | None = None
    xml_mtime: str | None = None
    has_file: bool = False
    checked: bool = False


class FileMatrixOut(BaseModel):
    """Ответ `GET /files/matrix`: строки и счётчики «файл есть у N из M»."""

    filename: str
    rows: list[FileMatrixRow] = Field(default_factory=list)
    total: int = 0
    present: int = 0
    missing_current: int = 0


class BulkActionJobOut(BaseModel):
    """Результат массового действия по одному магазину (§11.4: прогресс-таблица)."""

    store_id: int
    store_name: str
    job_id: int | None = None
    status: str = CommandStatus.QUEUED
    detail: str | None = None


class ActionOut(BaseModel):
    """Ответ одиночного действия: задача + предупреждение (Q53)."""

    job: JobOut
    message: str
    warning: str | None = None


class BulkActionOut(BaseModel):
    """Ответ массового действия: по строке на магазин."""

    action: str
    message: str
    total: int = 0
    jobs: list[BulkActionJobOut] = Field(default_factory=list)
    warning: str | None = None


# --- Вспомогательные функции ------------------------------------------------


def _get_store(session: Session, store_id: int) -> Store:
    """Магазин по id или понятный `404` (§5.2.2, Приложение B)."""
    store = session.get(Store, store_id)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Магазин id={store_id} не найден"
        )
    return store


def _validate_filename(value: str) -> str:
    """Имя файла без разделителей пути (I-2): правило берём из схемы агента."""
    try:
        return CheckFilesParams(filename=value).filename
    except ValueError as exc:
        raise HTTPException(
            status_code=HTTP_422, detail=str(exc)
        ) from exc


def _validate_action(action: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Проверить действие и его параметры; вернуть тип задачи и чистые параметры.

    Пароль 1С здесь принципиально не принимается: реквизиты подключения сервер
    берёт из карточки магазина, а пароль доставляет отдельно (П-1). Любое
    присланное значение `password` отбрасывается `sanitize_params()` (I-7).
    """
    name = (action or "").strip()
    if name not in ACTION_KIND:
        raise HTTPException(
            status_code=HTTP_422,
            detail=f"Неизвестное действие {action!r}",
        )

    raw = dict(params or {})
    clean: dict[str, Any] = {}

    if name == str(ActionKind.CHECK_FILES):
        filename = str(raw.get("filename") or "").strip()
        if filename:
            clean["filename"] = _validate_filename(filename)

    elif name == str(ActionKind.DOWNLOAD_FILE):
        url = str(raw.get("url-file") or raw.get("url_file") or "").strip()
        if not url:
            raise HTTPException(
                status_code=HTTP_422,
                detail="Не передана ссылка на файл (поле url-file)",
            )
        clean["url-file"] = url

    elif name == str(ActionKind.SEND_MESSAGE):
        text = str(raw.get("text_message") or "").strip()
        if not text:
            raise HTTPException(
                status_code=HTTP_422,
                detail="Текст сообщения не может быть пустым",
            )
        clean["text_message"] = text

    elif name == str(ActionKind.RUN_MERGE):
        file_name = str(raw.get("file_name") or "").strip()
        if not file_name:
            raise HTTPException(
                status_code=HTTP_422,
                detail="Не указано имя файла слияния (file_name)",
            )
        clean["file_name"] = _validate_filename(file_name)
        clean["full_merge"] = bool(raw.get("full_merge"))
        if raw.get("is_extension") is not None:
            clean["is_extension"] = bool(raw.get("is_extension"))
        extension_name = str(raw.get("extension_name") or "").strip()
        if extension_name:
            clean["extension_name"] = extension_name
        settings_file_name = str(raw.get("settings_file_name") or "").strip()
        if settings_file_name:
            clean["settings_file_name"] = _validate_filename(settings_file_name)

    elif name == str(ActionKind.RUN_UPDATE_DB):
        clean["is_fast_update"] = bool(raw.get("is_fast_update"))
        if raw.get("is_extension") is not None:
            clean["is_extension"] = bool(raw.get("is_extension"))
        extension_name = str(raw.get("extension_name") or "").strip()
        if extension_name:
            clean["extension_name"] = extension_name

    elif name == str(ActionKind.DELETE_FILE):
        file_name = str(raw.get("file_name") or raw.get("filename") or "").strip()
        if not file_name:
            raise HTTPException(
                status_code=HTTP_422,
                detail="Не указано имя файла обновления (file_name)",
            )
        clean["file_name"] = _validate_filename(file_name)
        suffix = str(raw.get("suffix") or "").strip().lower()
        if suffix:
            if not suffix.startswith("."):
                suffix = f".{suffix}"
            if suffix not in UPDATE_FILE_SUFFIXES:
                raise HTTPException(
                    status_code=HTTP_422,
                    detail=(
                        f"Недопустимое расширение {suffix!r}: "
                        f"ожидается {', '.join(UPDATE_FILE_SUFFIXES)}"
                    ),
                )
            clean["suffix"] = suffix

    elif name == str(ActionKind.NOTIFY):
        # Уведомление — не параметр задачи 1С, а отдельная сущность (§11.5):
        # здесь только проверяем форму, строку `notifications` создаёт вызвавшая
        # точка через `_create_notification_job()`.
        try:
            clean.update(notification_service.normalize_params(raw))
        except notification_service.NotificationValidationError as exc:
            raise HTTPException(status_code=HTTP_422, detail=str(exc)) from exc

    return ACTION_KIND[name], command_service.sanitize_params(clean)


def resolve_target_stores(
    session: Session,
    *,
    store_ids: list[int] | None = None,
    cluster: str | None = None,
    all_stores: bool = False,
) -> list[Store]:
    """Выбрать магазины для массовой операции (§11.4: выбор / кластер / все).

    Приоритет источников: список id → кластер → «все». Неактивные магазины
    (мягко удалённые) в выборку попадают, но действие по ним пропускается: так
    админ видит в прогрессе, что магазин отключён, а не «ничего не произошло».
    """
    stmt = select(Store).order_by(Store.name)
    if store_ids:
        stmt = stmt.where(Store.id.in_([int(item) for item in store_ids]))
    elif (cluster or "").strip():
        stmt = stmt.where(Store.cluster_name == cluster.strip())
    elif not all_stores:
        return []
    return list(session.scalars(stmt).all())


def _merge_warning(
    session: Session,
    store: Store,
    *,
    file_name: str,
    extension_name: str | None,
    is_extension: bool | None = None,
) -> str | None:
    """Предупреждение об отсутствии файла на кассе — Q53 (не блокирует запуск).

    Источник — последний снимок `file_checks` (§4.5), поэтому проверка ничего не
    стоит: за свежестью следит админ кнопкой «Проверить файлы» (§11.3).

    FR-4/Д-16/Д-17: тип слияния решает `is_extension_operation` (явный флаг →
    `.cf`/`.cfe` в имени → `extension_name`), а `extension_name` подставляется из
    карточки магазина только когда слияние идёт на расширение — ровно то же
    правило, что в `dispatcher.build_1c_payload` при фактической отправке. Иначе
    предупреждение проверяло бы `.cf` и лгало про расширение (и наоборот).
    """
    stem, _ = split_merge_file_name(file_name)
    if is_extension_operation(
        is_extension, file_name, extension_name or store.extension_name
    ):
        column = "cfe"
    else:
        column = "cf"
    stmt = (
        select(FileCheck)
        .where(FileCheck.store_id == store.id, FileCheck.filename == stem)
        .order_by(FileCheck.id.desc())
        .limit(1)
    )
    snapshot = session.scalars(stmt).first()
    if snapshot is None:
        return (
            f"Файлы «{file_name}» на кассе ещё не проверялись: "
            "сначала выполните «Проверить файлы»"
        )
    if getattr(snapshot, f"{column}_exists"):
        return None
    checked_at = to_iso_z(snapshot.checked_at) or "—"
    return (
        f"На кассе нет файла {stem}.{column} (последняя проверка {checked_at}): "
        "слияние может завершиться ошибкой"
    )


def _last_messages(session: Session, command_ids: list[int]) -> dict[int, str]:
    """Последняя строка журнала по каждой задаче — для списка `/jobs` (один запрос)."""
    if not command_ids:
        return {}
    rows = session.execute(
        select(JobLog.command_id, JobLog.message)
        .where(JobLog.command_id.in_(command_ids))
        .order_by(JobLog.id)
    ).all()
    return {int(command_id): str(message) for command_id, message in rows}


def _notify_audit_details(params: Mapping[str, Any]) -> dict[str, Any]:
    """Параметры уведомления, безопасные для журнала аудита (JSON, §4.10).

    Дедлайн в параметрах — `datetime`, а `audit_logs.details` — JSON-колонка:
    положить туда объект `datetime` нельзя, поэтому дата переводится в ISO-8601.
    """
    safe: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, datetime):
            safe[key] = to_iso_z(value)
        else:
            safe[key] = value
    return safe


def _create_notification_job(
    session: Session,
    params: dict[str, Any],
    *,
    stores: list[Store],
    author: str,
) -> tuple[int, dict[str, Any]]:
    """Создать уведомление под действие `notify` (§11.5) и параметры задачи.

    `notify` — единственное действие, которое рождает не только задачу, но и
    сущность `notifications`: разослать уведомление можно и из раздела
    `/notifications`, и из карточки магазина (§11.2, §11.5). Задача рассылки
    ссылается на уведомление по id, а адресация уже зафиксирована в
    `notification_targets`, поэтому `target_type` здесь всегда `stores`.
    """
    notification = notification_service.create_notification(
        session,
        title=str(params.get("title") or ""),
        body=str(params.get("body") or ""),
        severity=str(params.get("severity") or "info"),
        requires_ack=bool(params.get("requires_ack")),
        target_type=str(TargetType.STORES),
        stores=stores,
        deadline_at=params.get("deadline_at"),
        repeat_every_min=params.get("repeat_every_min"),
        category_id=params.get("category_id"),
        created_by=author,
    )
    return notification.id, {"notification_id": notification.id}


def _create_job(
    session: Session,
    store: Store,
    *,
    kind: str,
    params: dict[str, Any],
    author: str,
    action_name: str,
) -> Command:
    """Поставить задачу в очередь и объяснить это в её журнале (§4.3, §11.6)."""
    command = command_service.create_command(
        session, store_id=store.id, kind=kind, params=params, created_by=author
    )
    command.store = store
    command_service.add_log(
        session,
        command,
        f"Задача создана ({action_name}) администратором {author}; отправку выполнит диспетчер",
    )
    return command


# --- Запрет одновременного обновления одной ИБ (R12, FR-28, П-5) --------------


def _guard_update(session: Session, stores: list[Store], *, kind: str) -> None:
    """Отказать в запуске, если он нарушает запрет R12 (FR-28, П-5).

    Вызывается **внутри** `command_service.dispatch_guard()`: проверка и создание
    задач обязаны быть одним критическим участком (§11.4), иначе два админа,
    нажавшие кнопку одновременно, проскочили бы запрет.

    Проверяются только действия, меняющие ИБ: «Проверить файлы», «Скачать» и
    «Сообщение» не блокируются — они серверную базу не трогают.
    """
    if kind not in command_service.UPDATE_KINDS:
        return

    conflicts = command_service.find_update_conflicts(session, stores)
    if not conflicts:
        return

    reasons = " ".join(
        f"{number}) {item.message}." for number, item in enumerate(conflicts, start=1)
    )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            "Запуск отменён: нарушен запрет одновременного обновления одной серверной ИБ "
            f"(R12), задачи не созданы. {reasons}"
        ),
    )


def _dispatch_bulk(
    session: Session,
    stores: list[Store],
    *,
    kind: str,
    params: dict[str, Any],
    author: str,
    action_name: str,
) -> tuple[list[BulkActionJobOut], str | None]:
    """Расставить задачи по магазинам и собрать строки прогресса (§11.4).

    Идёт внутри `command_service.dispatch_guard()`. Отключённый магазин даёт
    строку `skipped`: админ видит, что магазин пропущен, а не «ничего не вышло».
    """
    results: list[BulkActionJobOut] = []
    warning: str | None = None

    for store in stores:
        if not store.is_active:
            results.append(
                BulkActionJobOut(
                    store_id=store.id,
                    store_name=store.name,
                    status="skipped",
                    detail="Магазин отключён (is_active=false)",
                )
            )
            continue
        command = _create_job(
            session,
            store,
            kind=kind,
            params=params,
            author=author,
            action_name=action_name,
        )
        results.append(
            BulkActionJobOut(
                store_id=store.id,
                store_name=store.name,
                job_id=command.id,
                status=str(command.status),
                detail=dispatcher_hint(store, command),
            )
        )
        if kind == str(CommandKind.RUN_MERGE) and warning is None:
            warning = _merge_warning(
                session,
                store,
                file_name=str(params.get("file_name") or ""),
                extension_name=params.get("extension_name"),
                is_extension=params.get("is_extension"),
            )

    return results, warning


# --- Одиночное действие (§5.2.2, §11.4) -------------------------------------


@router.post(
    "/stores/{store_id}/actions",
    response_model=ActionOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Действие над магазином",
)
def store_action(  # noqa: PLR0913 - параметры HTTP-точки
    store_id: int,
    payload: ActionRequest,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> ActionOut:
    """Поставить операцию магазину: ответ `202`, работу выполняет диспетчер (§3.3).

    `202` здесь — не «уже выполнено», а «принято в работу» (Приложение B): сервер
    не держит HTTP-запрос открытым, пока касса обновляет конфигурацию.
    """
    store = _get_store(session, store_id)
    if not store.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Магазин отключён: операции по нему не выполняются",
        )

    kind, params = _validate_action(payload.action, payload.params)
    # Э5: действие `notify` сначала создаёт уведомление, а уже потом задачу (§11.5)
    audit_details: dict[str, Any] = {"action": payload.action}
    if kind == str(CommandKind.NOTIFY):
        notify_params = _notify_audit_details(params)
        notification_id, params = _create_notification_job(
            session, params, stores=[store], author=admin.username
        )
        audit_details["notification_id"] = notification_id
        audit_details["notify"] = notify_params

    with command_service.dispatch_guard():
        _guard_update(session, [store], kind=kind)
        command = _create_job(
            session,
            store,
            kind=kind,
            params=params,
            author=admin.username,
            action_name=payload.action,
        )

    warning: str | None = None
    if kind == str(CommandKind.RUN_MERGE):
        warning = _merge_warning(
            session,
            store,
            file_name=str(params.get("file_name") or ""),
            extension_name=params.get("extension_name"),
            is_extension=params.get("is_extension"),
        )

    audit_service.record(
        session,
        action="store_action",
        actor=admin.username,
        entity="store",
        entity_id=store.id,
        details={
            **audit_details,
            "job_id": command.id,
            "params": params,
            "warning": bool(warning),
        },
        ip_address=get_client_ip(request),
    )
    session.flush()

    return ActionOut(
        job=JobOut.from_model(command, last_message=None),
        message=(
            f"Задача #{command.id} поставлена в очередь: "
            f"{dispatcher_hint(store, command)}"
        ),
        warning=warning,
    )


def dispatcher_hint(store: Store, command: Command) -> str:
    """Пояснение для UI: что произойдёт дальше (офлайн-касса — не ошибка, §11.4)."""
    if command.status == str(CommandStatus.QUEUED):
        return f"магазин «{store.name}» получит её при следующем опросе диспетчера"
    return "задача отправлена на кассу"


# --- Массовое действие (§5.2.2, §11.4) -------------------------------------


@router.post(
    "/bulk/actions",
    response_model=BulkActionOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Массовое действие",
)
def bulk_action(  # noqa: PLR0913 - параметры HTTP-точки
    payload: BulkActionRequest,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> BulkActionOut:
    """Поставить одну и ту же операцию пачке магазинов (§11.4).

    Выбор: `store_ids` (чекбоксы) → `cluster` (весь кластер) → `all: true`.
    По каждому магазину возвращается строка прогресса: `queued` или `skipped`
    (товар отключён) — ровно то, что рисует прогресс-таблица в UI.
    """
    kind, params = _validate_action(payload.action, payload.params)
    stores = resolve_target_stores(
        session,
        store_ids=payload.store_ids,
        cluster=payload.cluster,
        all_stores=payload.all,
    )
    if not stores:
        raise HTTPException(
            status_code=HTTP_422,
            detail="Не выбрано ни одного магазина (store_ids / cluster / all)",
        )

    # Э5: одно уведомление на всю выборку — адресат уже зафиксирован (§11.5)
    audit_details: dict[str, Any] = {"action": payload.action}
    if kind == str(CommandKind.NOTIFY):
        targets = [item for item in stores if item.is_active]
        if not targets:
            raise HTTPException(
                status_code=HTTP_422,
                detail="Все выбранные магазины отключены: рассылать некому",
            )
        notify_params = _notify_audit_details(params)
        notification_id, params = _create_notification_job(
            session, params, stores=targets, author=admin.username
        )
        audit_details["notification_id"] = notification_id
        audit_details["notify"] = notify_params

    with command_service.dispatch_guard():
        _guard_update(session, stores, kind=kind)
        results, warning = _dispatch_bulk(
            session,
            stores,
            kind=kind,
            params=params,
            author=admin.username,
            action_name=payload.action,
        )
    created = sum(1 for item in results if item.job_id is not None)

    audit_service.record(
        session,
        action="bulk_action",
        actor=admin.username,
        entity="stores",
        entity_id=None,
        details={
            **audit_details,
            "params": params,
            "stores": [item.store_id for item in results],
            "created": created,
        },
        ip_address=get_client_ip(request),
    )
    session.flush()

    return BulkActionOut(
        action=payload.action,
        message=f"Задачи поставлены: {created} из {len(results)} магазинов",
        total=len(results),
        jobs=results,
        warning=warning,
    )


# --- История задач (§11.6) --------------------------------------------------


@router.get("/jobs", response_model=JobsPageOut, summary="История операций")
def list_jobs(  # noqa: PLR0913 - параметры HTTP-точки
    session: Annotated[Session, Depends(get_db)],
    store_id: Annotated[int | None, Query(ge=1)] = None,
    local_base_id: Annotated[int | None, Query(ge=1)] = None,
    kind: Annotated[str | None, Query(max_length=32)] = None,
    job_status: Annotated[str | None, Query(alias="status", max_length=32)] = None,
    active_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobsPageOut:
    """Список задач с фильтрами: магазин, локальная база, тип, статус, «только в работе» (§11.6)."""
    commands = command_service.list_commands(
        session,
        store_id=store_id,
        local_base_id=local_base_id,
        kind=(kind or "").strip() or None,
        status=(job_status or "").strip() or None,
        active_only=active_only,
        limit=limit,
        offset=offset,
    )
    messages = _last_messages(session, [command.id for command in commands])
    counts = command_service.counts_by_status(session, store_id=store_id, local_base_id=local_base_id)
    return JobsPageOut(
        items=[
            JobOut.from_model(command, last_message=messages.get(command.id))
            for command in commands
        ],
        counts=counts,
        total=sum(counts.values()),
        active=sum(counts.get(status_name, 0) for status_name in command_service.ACTIVE_STATUSES),
    )


@router.get("/jobs/{job_id}", response_model=JobDetailOut, summary="Задача и полный журнал")
def get_job(job_id: int, session: Annotated[Session, Depends(get_db)]) -> JobDetailOut:
    """Карточка задачи `/jobs/{id}`: параметры, статус, полный журнал (§11.6)."""
    command = command_service.get_command(session, job_id)
    if command is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Задача id={job_id} не найдена"
        )
    logs = list(command.logs)
    base = JobOut.from_model(command, last_message=logs[-1].message if logs else None)
    return JobDetailOut(**base.model_dump(), logs=[JobLogOut.from_model(entry) for entry in logs])


@router.delete(
    "/jobs/{job_id}",
    response_model=JobDeleteOut,
    summary="Удалить задачу",
)
def delete_job(
    job_id: int,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> JobDeleteOut:
    """Удалить одну задачу вместе с её журналом (§11.6, Э10).

    Зависшая задача в очереди (например, ошибочный `download_file`) блокирует
    новые — админ убирает её вручную. Для незавершённой это «снять с очереди»:
    диспетчер больше её не увидит. Удаление в любом статусе, историю аудита
    оставляет `job_delete`.
    """
    deleted = command_service.delete_command(session, job_id)
    if deleted is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Задача id={job_id} не найдена"
        )
    audit_service.record(
        session,
        action="job_delete",
        actor=admin.username,
        entity="job",
        entity_id=job_id,
        details={"kind": str(deleted.kind), "status": str(deleted.status)},
        ip_address=get_client_ip(request),
    )
    session.commit()
    logger.info("Задача id=%s удалена администратором %s", job_id, admin.username)
    return JobDeleteOut(job_id=job_id, deleted=True, message="Задача удалена")


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=dict[str, str | int],
    summary="Прервать задачу",
)
def cancel_job(
    job_id: int,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> dict[str, str | int]:
    """Прервать незавершённую задачу (Д-22): `queued`/`sent`/`running` → `canceled`.

    Задача остаётся в истории со статусом «отменена» (в отличие от удаления),
    диспетчер больше её не опрашивает и не ретраит. Агент, если команда уже
    ушла, может продолжать выполнение на кассе — это «снять с контроля сервера».
    """
    command = command_service.get_command(session, job_id)
    if command is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Задача id={job_id} не найдена"
        )

    if str(command.status) not in command_service.ACTIVE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Задача id={job_id} уже завершена (статус: {command.status})",
        )

    command_service.set_status(session, command, CommandStatus.CANCELED)
    command_service.add_log(
        session, command, f"Задача прервана администратором {admin.username}"
    )
    audit_service.record(
        session,
        action="job_cancel",
        actor=admin.username,
        entity="job",
        entity_id=job_id,
        details={"kind": str(command.kind), "previous_status": str(command.status)},
        ip_address=get_client_ip(request),
    )
    session.commit()
    logger.info("Задача id=%s прервана администратором %s", job_id, admin.username)
    return {"job_id": job_id, "status": str(command.status), "message": "Задача отменена"}


@router.post(
    "/jobs/bulk-delete",
    response_model=JobsBulkDeleteOut,
    summary="Удалить выбранные задачи",
)
def bulk_delete_jobs(
    payload: JobsBulkDeleteRequest,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> JobsBulkDeleteOut:
    """Удалить несколько задач за один запрос (§11.6, Э10).

    Тело — `{"ids": [...]}`. Несуществующие id пропускаются, поэтому повторный
    запрос идемпотентен. Пустой список — `422` (чтобы UI не «удалил 0» по ошибке).
    В аудите остаётся запись `job_bulk_delete` с числом удалённых.
    """
    ids = list(dict.fromkeys(int(item) for item in payload.ids))
    if not ids:
        raise HTTPException(
            status_code=HTTP_422,
            detail="Не выбрано ни одной задачи для удаления",
        )
    deleted = command_service.delete_commands(session, ids)
    audit_service.record(
        session,
        action="job_bulk_delete",
        actor=admin.username,
        entity="job",
        entity_id=None,
        details={"ids": ids, "deleted": deleted, "total": len(ids)},
        ip_address=get_client_ip(request),
    )
    session.commit()
    logger.info(
        "Администратор %s удалил задач: %s из %s", admin.username, deleted, len(ids)
    )
    return JobsBulkDeleteOut(
        deleted=deleted,
        total=len(ids),
        message=f"Удалено задач: {deleted} из {len(ids)}",
    )


# --- Матрица файлов (§11.3) -------------------------------------------------


@router.get("/files/matrix", response_model=FileMatrixOut, summary="Матрица файлов обновления")
def files_matrix(  # noqa: PLR0913 - параметры HTTP-точки
    session: Annotated[Session, Depends(get_db)],
    filename: Annotated[str | None, Query(max_length=200)] = None,
    store_id: Annotated[int | None, Query(ge=1)] = None,
    cluster: Annotated[str | None, Query(max_length=200)] = None,
    include_inactive: Annotated[bool, Query()] = False,
) -> FileMatrixOut:
    """Матрица «магазин × (cf/cfe/xml)»: последний снимок на магазин (§4.5, §11.3).

    Дополнительно отдаются счётчики `present` / `missing_current`, из которых UI
    рисует «файл есть у N из M» и красную подсветку магазинов без файла (Q53).
    """
    name = (filename or "").strip() or settings_service.get_str(session, "default_file_name")
    _validate_filename(name)

    stores = resolve_target_stores(
        session,
        store_ids=[store_id] if store_id else None,
        cluster=cluster,
        all_stores=True,
    )
    if not include_inactive:
        visible = [store for store in stores if store.is_active]
    else:
        visible = stores

    snapshots: dict[int, FileCheck] = {}
    if visible:
        rows = session.scalars(
            select(FileCheck)
            .where(
                FileCheck.store_id.in_([store.id for store in visible]),
                FileCheck.filename == name,
            )
            .order_by(FileCheck.id)
        ).all()
        for snapshot in rows:  # последний снимок перезаписывает предыдущий (§4.5)
            snapshots[int(snapshot.store_id)] = snapshot

    window = settings_service.get_int(session, "online_window_sec")
    now = utcnow()
    matrix: list[FileMatrixRow] = []
    for store in visible:
        snapshot = snapshots.get(store.id)
        exists = bool(
            snapshot
            and (snapshot.cf_exists or snapshot.cfe_exists or snapshot.xml_exists)
        )
        matrix.append(
            FileMatrixRow(
                store_id=store.id,
                store_name=store.name,
                ip_address=store.ip_address,
                cluster_name=store.cluster_name,
                is_active=store.is_active,
                is_online=store.is_online(now, window),
                filename=name,
                checked_at=to_iso_z(snapshot.checked_at) if snapshot else None,
                cf_exists=bool(snapshot.cf_exists) if snapshot else False,
                cfe_exists=bool(snapshot.cfe_exists) if snapshot else False,
                xml_exists=bool(snapshot.xml_exists) if snapshot else False,
                cf_size=snapshot.cf_size if snapshot else None,
                cfe_size=snapshot.cfe_size if snapshot else None,
                xml_size=snapshot.xml_size if snapshot else None,
                cf_mtime=to_iso_z(snapshot.cf_mtime) if snapshot else None,
                cfe_mtime=to_iso_z(snapshot.cfe_mtime) if snapshot else None,
                xml_mtime=to_iso_z(snapshot.xml_mtime) if snapshot else None,
                has_file=exists,
                checked=snapshot is not None,
            )
        )

    present = sum(1 for row in matrix if row.has_file)
    return FileMatrixOut(
        filename=name,
        rows=matrix,
        total=len(matrix),
        present=present,
        missing_current=len(matrix) - present,
    )





# --- Удаление снимков файлов (Э10) ------------------------------------------


class FileSnapshotDeletedOut(BaseModel):
    """Ответ `DELETE /files/{store_id}`: снимки проверки убраны из матрицы (§11.3)."""

    store_id: int
    store_name: str
    filename: str | None = None
    removed: int = 0
    message: str


@router.delete(
    "/files/{store_id}",
    response_model=FileSnapshotDeletedOut,
    summary="Убрать снимок файлов из матрицы (Э10)",
)
def delete_file_snapshot(
    store_id: int,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
    filename: Annotated[str | None, Query(max_length=200)] = None,
) -> FileSnapshotDeletedOut:
    """Удалить снимок(и) `file_checks` магазина (Э10, §4.5, Q52).

    Матрица `/files` рисует **последний** снимок на магазин: после удаления строка
    снова «не проверялся», пока касса не пришлёт новый снимок (§11.3). Файлы на
    кассе это не трогает — для них есть отдельное действие `delete_file`.
    """
    store = _get_store(session, store_id)
    name: str | None = None
    if filename is not None and filename.strip():
        name = _validate_filename(filename.strip())

    stmt = delete(FileCheck).where(FileCheck.store_id == store.id)
    if name is not None:
        stmt = stmt.where(FileCheck.filename == name)
    removed = int(session.execute(stmt).rowcount or 0)

    audit_service.record(
        session,
        action="file_snapshot_delete",
        actor=admin.username,
        entity="store",
        entity_id=store.id,
        details={"filename": name, "removed": removed},
        ip_address=get_client_ip(request),
    )
    session.commit()
    logger.info(
        "Снимки файлов магазина id=%s удалены (имя: %s): строк %s",
        store.id,
        name or "все",
        removed,
    )
    return FileSnapshotDeletedOut(
        store_id=store.id,
        store_name=store.name,
        filename=name,
        removed=removed,
        message=(
            f"Снимок проверки убран из матрицы: строк {removed}"
            if removed
            else "Снимков не было — матрица и так пустая для этого магазина"
        ),
    )

