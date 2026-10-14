"""API уведомлений `/api/v1/admin/notifications*` (§5.2.2, §11.5) — состав Э5.

Что закрывает модуль (ФН3):

* `POST /notifications` — создание и рассылка: уведомление адресуется всем,
  кластеру или списку магазинов, а доставку выполняет диспетчер задачами
  `notify` (§11.4) — выключенная касса получает уведомление позже, а не теряет;
* `GET /notifications` — список с фильтрами и прогрессом «ознакомлены X из Y»;
* `GET /notifications/{id}` и `GET /notifications/{id}/report` — отчёт
  «магазины → сессии» и его выгрузка CSV (`?format=csv`);
* `POST /notifications/{id}/cancel` — остановка рассылки (§11.5).

Настройка `notification_snooze_min` (§4.9) уезжает на кассу вместе с
уведомлением: это часть контракта UI — интервал возврата отложенного видно в
списке и в отчёте (П-2).

Роутер защищён `require_active_admin`: «новый эндпоинт по умолчанию защищён»,
как в `server/api/stores.py` и `server/api/devices.py`.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from server.core.dependencies import get_client_ip, get_db, require_active_admin
from server.db.models import AdminUser, Notification
from server.services import audit_service, command_service, notification_service
from shared.enums import CommandKind, NotificationSeverity, TargetType
from shared.timeutil import to_iso_z

logger = logging.getLogger(__name__)

__all__ = ["router"]

router = APIRouter(tags=["admin-notifications"], dependencies=[Depends(require_active_admin)])

#: 422 без deprecation-предупреждения Starlette — как в `admin.py` и `operations.py`
HTTP_422 = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)

#: Имя файла выгрузки отчёта (§11.5): администратор получает понятное имя
CSV_FILENAME_TEMPLATE = "notification-{id}.csv"


class NotificationCreateIn(BaseModel):
    """Тело `POST /notifications` (§11.5, §5.2.2)."""

    title: str
    body: str
    severity: str = str(NotificationSeverity.INFO)
    requires_ack: bool = False
    target_type: str = str(TargetType.ALL)
    store_ids: list[int] = Field(default_factory=list)
    cluster: str | None = None
    deadline_at: str | None = None
    repeat_every_min: int | None = None
    use_msg_exe: bool = True


class NotificationOut(BaseModel):
    """Уведомление в списке `/notifications` (§11.5)."""

    id: int
    title: str
    body: str
    severity: str
    requires_ack: bool
    target_type: str
    target_value: str | None = None
    deadline_at: str | None = None
    repeat_every_min: int | None = None
    use_msg_exe: bool
    is_active: bool
    state: str
    created_by: str | None = None
    created_at: str | None = None
    store_names: list[str] = Field(default_factory=list)
    totals: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def from_model(cls, notification: Notification) -> "NotificationOut":
        """Собрать строку списка: статус и прогресс считаются по факту (§4.8)."""
        return cls(
            id=notification.id,
            title=notification.title,
            body=notification.body,
            severity=str(notification.severity),
            requires_ack=bool(notification.requires_ack),
            target_type=str(notification.target_type),
            target_value=notification.target_value,
            deadline_at=to_iso_z(notification.deadline_at),
            repeat_every_min=notification.repeat_every_min,
            use_msg_exe=bool(notification.use_msg_exe),
            is_active=bool(notification.is_active),
            state=notification_service.state(notification),
            created_by=notification.created_by,
            created_at=to_iso_z(notification.created_at),
            store_names=[
                target.store.name
                for target in notification.targets
                if target.store is not None
            ],
            totals=notification_service.progress(notification),
        )


class NotificationListOut(BaseModel):
    """Ответ списка: строки, счётчики по статусам и «ждут ознакомления» (§11.5)."""

    items: list[NotificationOut]
    total: int
    counts: dict[str, int]
    pending_receipts: int
    snooze_min: int


class NotificationCreateOut(BaseModel):
    """Ответ на создание: что разослано и сколько задач поставлено (§11.4)."""

    notification: NotificationOut
    stores: int
    jobs: int
    skipped_store_ids: list[int] = Field(default_factory=list)
    message: str


class CancelNotificationOut(BaseModel):
    """Ответ на отмену рассылки (§11.5)."""

    notification_id: int
    state: str
    canceled_jobs: int
    message: str



class ReceiptOut(BaseModel):
    """Строка «сессия Windows» в отчёте (§4.8, П-2)."""

    session_user: str
    session_id: str
    status: str
    shown_at: str | None = None
    acknowledged_at: str | None = None
    ack_comment: str | None = None
    snooze_count: int = 0
    snooze_until: str | None = None
    is_acknowledged: bool = False
    is_overdue: bool = False


class NotificationStoreReportOut(BaseModel):
    """Магазин в отчёте: доставка, состав сессий и их статусы (§11.5)."""

    store_id: int
    store_name: str | None = None
    cluster_name: str | None = None
    store_is_active: bool = True
    delivered_at: str | None = None
    sessions_count: int = 0
    total: int = 0
    acknowledged: int = 0
    receipts: list[ReceiptOut] = Field(default_factory=list)


class NotificationReportOut(BaseModel):
    """Отчёт по уведомлению для страницы `/notifications/{id}` (§11.5)."""

    notification: NotificationOut
    state: str
    deadline_at: str | None = None
    created_at: str | None = None
    totals: dict[str, int] = Field(default_factory=dict)
    snooze_min: int
    stores: list[NotificationStoreReportOut] = Field(default_factory=list)


# --- Вспомогательные функции -------------------------------------------------


def _get(session: Session, notification_id: int) -> Notification:
    """Уведомление по id или `404` (§5.2.2)."""
    try:
        return notification_service.get_notification(session, notification_id)
    except notification_service.NotificationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


def _report(session: Session, notification_id: int) -> NotificationReportOut:
    """Собрать отчёт «X из Y» по магазинам и сессиям (§11.5)."""
    notification = _get(session, notification_id)
    data = notification_service.report(session, notification)
    stores = data.get("stores")
    return NotificationReportOut(
        notification=NotificationOut.from_model(notification),
        state=str(data.get("state") or ""),
        deadline_at=data.get("deadline_at"),  # type: ignore[arg-type]
        created_at=data.get("created_at"),  # type: ignore[arg-type]
        totals=dict(data.get("totals") or {}),  # type: ignore[arg-type]
        snooze_min=notification_service.snooze_min(session),
        stores=[
            NotificationStoreReportOut.model_validate(item)
            for item in (stores if isinstance(stores, list) else [])
        ],
    )


def _create_jobs(session: Session, notification: Notification, *, author: str) -> int:
    """Поставить задачу `notify` на каждый магазин (§11.4, FR-16).

    Задачи создаются в том же критическом участке, что и проверки R12
    (`dispatch_guard`): админская кнопка не должна обгонять диспетчер.
    """
    with command_service.dispatch_guard():
        for target in list(notification.targets):
            command = command_service.create_command(
                session,
                store_id=target.store_id,
                kind=CommandKind.NOTIFY,
                params={"notification_id": notification.id},
                created_by=author,
            )
            command.store = target.store
            command_service.add_log(
                session,
                command,
                (
                    f"Рассылка уведомления «{notification.title}» "
                    f"(id={notification.id}): отправку выполнит диспетчер"
                ),
            )
    session.flush()
    return len(list(notification.targets))



# --- Создание и рассылка (§11.5) -------------------------------------------


@router.post(
    "/notifications",
    response_model=NotificationCreateOut,
    status_code=status.HTTP_201_CREATED,
    summary="Создать уведомление и разослать его",
)
def create_notification(
    payload: NotificationCreateIn,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
    request: Request,
) -> NotificationCreateOut:
    """Создать уведомление по форме `/notifications` (§11.5).

    Доставку выполняет диспетчер: на каждый магазин ставится задача `notify`,
    поэтому выключенная касса получает уведомление, как только вернётся в сеть
    (§11.4, FR-16). Отключённые магазины в адресацию не попадают — их id
    возвращаются в `skipped_store_ids`, чтобы админ видел причину.
    """
    try:
        params = notification_service.normalize_params(payload.model_dump())
        stores = notification_service.target_stores(
            session,
            target_type=str(params["target_type"]),
            store_ids=params["store_ids"],  # type: ignore[arg-type]
            cluster=params["cluster"],  # type: ignore[arg-type]
        )
    except notification_service.NotificationValidationError as exc:
        raise HTTPException(status_code=HTTP_422, detail=str(exc)) from exc

    if not stores:
        raise HTTPException(
            status_code=HTTP_422,
            detail="Нет магазинов-получателей: проверьте адресацию и активные магазины",
        )

    notification = notification_service.create_notification(
        session,
        title=str(params["title"]),
        body=str(params["body"]),
        severity=str(params["severity"]),
        requires_ack=bool(params["requires_ack"]),
        target_type=str(params["target_type"]),
        stores=stores,
        cluster=params["cluster"],  # type: ignore[arg-type]
        deadline_at=params["deadline_at"],  # type: ignore[arg-type]
        repeat_every_min=params["repeat_every_min"],  # type: ignore[arg-type]
        use_msg_exe=bool(params["use_msg_exe"]),
        created_by=admin.username,
    )
    jobs = _create_jobs(session, notification, author=admin.username)
    out = NotificationOut.from_model(notification)
    requested = {int(item) for item in payload.store_ids}
    skipped = sorted(requested - {store.id for store in stores}) if requested else []

    audit_service.record(
        session,
        action="notification_create",
        actor=admin.username,
        entity="notification",
        entity_id=notification.id,
        details={
            "severity": notification.severity,
            "target_type": notification.target_type,
            "requires_ack": bool(notification.requires_ack),
            "stores": len(stores),
        },
        ip_address=get_client_ip(request),
    )
    session.commit()
    logger.info(
        "Уведомление id=%s создано администратором %s: магазинов %s, задач %s",
        notification.id,
        admin.username,
        len(stores),
        jobs,
    )
    return NotificationCreateOut(
        notification=out,
        stores=len(stores),
        jobs=jobs,
        skipped_store_ids=skipped,
        message=(
            f"Уведомление создано и поставлено в рассылку: магазинов {len(stores)}, "
            f"задач {jobs}"
        ),
    )



@router.get("/notifications", response_model=NotificationListOut, summary="Список уведомлений")
def list_notifications(  # noqa: PLR0913 - параметры HTTP-точки
    session: Annotated[Session, Depends(get_db)],
    status_filter: Annotated[
        str | None,
        Query(alias="status", pattern="^(all|active|done|canceled)$"),
    ] = None,
    severity: Annotated[str | None, Query(max_length=16)] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    store_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = notification_service.DEFAULT_LIST_LIMIT,
) -> NotificationListOut:
    """Уведомления с фильтрами и прогрессом «ознакомлены X из Y» (§11.5).

    Счётчики статусов считаются по **всем** уведомлениям, а не по отфильтрованным:
    админу нужен ответ «сколько сейчас в работе», а не «сколько в текущем срезе».
    `store_id` нужен карточке магазина: там показываются только его уведомления
    (§11.2).
    """
    try:
        items = notification_service.list_notifications(
            session, status=status_filter, severity=severity, search=search, limit=limit
        )
        everything = notification_service.list_notifications(session, limit=limit)
    except notification_service.NotificationValidationError as exc:
        raise HTTPException(status_code=HTTP_422, detail=str(exc)) from exc

    if store_id is not None:
        items = [
            item
            for item in items
            if any(int(target.store_id) == int(store_id) for target in item.targets)
        ]

    counts: dict[str, int] = {"active": 0, "done": 0, "canceled": 0}
    for item in everything:
        current = notification_service.state(item)
        counts[current] = counts.get(current, 0) + 1

    return NotificationListOut(
        items=[NotificationOut.from_model(item) for item in items],
        total=len(items),
        counts=counts,
        pending_receipts=notification_service.pending_count(session),
        snooze_min=notification_service.snooze_min(session),
    )


@router.get(
    "/notifications/{notification_id}",
    response_model=NotificationReportOut,
    summary="Отчёт по уведомлению",
)
def get_notification(
    notification_id: int,
    session: Annotated[Session, Depends(get_db)],
) -> NotificationReportOut:
    """Отчёт «X из Y»: магазины → сессии (§11.5, §4.8)."""
    return _report(session, notification_id)



@router.get(
    "/notifications/{notification_id}/report",
    summary="Отчёт по уведомлению (JSON или CSV)",
)
def notification_report(
    notification_id: int,
    session: Annotated[Session, Depends(get_db)],
    fmt: Annotated[str | None, Query(alias="format", pattern="^(json|csv)$")] = None,
) -> Response:
    """Отчёт по уведомлению; `?format=csv` — выгрузка для отчётности (§11.5).

    CSV отдаётся файлом (`Content-Disposition`): администратор нажимает
    «Экспорт CSV» и получает понятное имя файла, а не вкладку с текстом.
    """
    report = _report(session, notification_id)
    if fmt == "csv":
        return Response(
            content=notification_service.report_csv(report.model_dump()),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{CSV_FILENAME_TEMPLATE.format(id=notification_id)}"'
                )
            },
        )
    return Response(content=report.model_dump_json(), media_type="application/json")


@router.post(
    "/notifications/{notification_id}/cancel",
    response_model=CancelNotificationOut,
    summary="Отменить рассылку уведомления",
)
def cancel_notification(
    notification_id: int,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
    request: Request,
) -> CancelNotificationOut:
    """Остановить рассылку (§11.5).

    Новые показы прекращаются (TrayAgent получает только активные уведомления,
    §7.4), отчёт сохраняется — видно, кто успел ознакомиться до отмены.
    Повторная отмена — `409`: состояние уже такое, повторять нечего.
    """
    notification = _get(session, notification_id)
    if not notification.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Уведомление уже отменено: рассылка не активна",
        )

    canceled = notification_service.cancel_notification(session, notification)
    audit_service.record(
        session,
        action="notification_cancel",
        actor=admin.username,
        entity="notification",
        entity_id=notification.id,
        details={"canceled_jobs": canceled},
        ip_address=get_client_ip(request),
    )
    session.commit()
    logger.info(
        "Уведомление id=%s отменено администратором %s (задач закрыто: %s)",
        notification.id,
        admin.username,
        canceled,
    )
    return CancelNotificationOut(
        notification_id=notification.id,
        state=notification_service.state(notification),
        canceled_jobs=canceled,
        message=f"Рассылка остановлена; незавершённых задач закрыто: {canceled}",
    )
