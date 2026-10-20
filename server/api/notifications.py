"""API уведомлений `/api/v1/admin/notifications*` (§5.2.2, §11.5) — состав Э5.

Что закрывает модуль (ФН3):

* `POST /notifications` — создание и рассылка: уведомление адресуется всем,
  кластеру или списку магазинов, а доставку выполняет диспетчер задачами
  `notify` (§11.4) — выключенная касса получает уведомление позже, а не теряет.
  Э9: тело может быть разметкой (`body_format`), вместе с созданием приезжают
  картинки тела (`images`, base64 — файлы ложатся до первой доставки) и признак
  «удалять после прочтения» (`delete_after_read`);
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

import base64
import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from server.config import API_PREFIX
from server.core.context import AppContext
from server.core.dependencies import (
    get_client_ip,
    get_context,
    get_db,
    require_active_admin,
)
from server.db.models import AdminUser, Notification, NotificationCategory, NotificationImage
from server.services import audit_service, command_service, notification_service
from shared import richtext
from shared.enums import CommandKind, NotificationSeverity, TargetType
from shared.timeutil import to_iso_z

logger = logging.getLogger(__name__)

__all__ = ["router"]

router = APIRouter(tags=["admin-notifications"], dependencies=[Depends(require_active_admin)])

#: 422 без deprecation-предупреждения Starlette — как в `admin.py` и `operations.py`
HTTP_422 = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)

#: Имя файла выгрузки отчёта (§11.5): администратор получает понятное имя
CSV_FILENAME_TEMPLATE = "notification-{id}.csv"

#: Адрес картинки тела для SPA (Э9): тот же путь отдаёт `GET .../images/{name}`
ADMIN_IMAGE_URL_TEMPLATE = API_PREFIX + "/admin/notifications/{notification_id}/images/{name}"

#: Префикс `data:`-URL из редактора: `data:image/png;base64,...`
DATA_URL_PREFIX = "data:"


class NotificationImageIn(BaseModel):
    """Картинка тела уведомления: `POST /notifications/{id}/images` и поле `images` при создании (§5.2.2, Э9).

    Картинка приходит строкой base64 (редактор отдаёт её как `data:`-URL):
    `python-multipart` в зависимостях сервера нет (§14.1), поэтому обходимся
    JSON и не тянем лишний пакет на стенд заказчика.
    """

    name: str
    data_base64: str


class NotificationCategoryIn(BaseModel):
    """Тело `POST/PUT /notification-categories` (§11.5, спека 1.22).

    Цвет — из палитры сервиса (`notification_service.CATEGORY_COLORS`): окно
    кассы и дашборд должны показывать одинаково читаемые плашки.
    """

    name: str
    color: str = ""
    sort_order: int = 0
    is_active: bool | None = None


class NotificationCategoryOut(BaseModel):
    """Категория уведомления в ответах админки (§11.5, спека 1.22)."""

    id: int
    name: str
    color: str
    sort_order: int = 0
    is_active: bool = True
    created_by: str | None = None
    created_at: str | None = None

    @classmethod
    def from_model(cls, category: NotificationCategory) -> "NotificationCategoryOut":
        """Собрать строку списка категорий (§11.5)."""
        return cls(
            id=int(category.id),
            name=str(category.name),
            color=str(category.color),
            sort_order=int(category.sort_order),
            is_active=bool(category.is_active),
            created_by=category.created_by,
            created_at=to_iso_z(category.created_at),
        )


class NotificationCreateIn(BaseModel):
    """Тело `POST /notifications` (§11.5, §5.2.2)."""

    title: str
    body: str
    #: Э9: `plain` — как было, `html` — разметка из редактора (санитайз на сервере)
    body_format: str = richtext.BODY_FORMAT_PLAIN
    severity: str = str(NotificationSeverity.INFO)
    #: 1.22: информационная категория («Продажи», «Закупки»); `None` — без категории
    category_id: int | None = None
    requires_ack: bool = False
    #: Э9: «удалять после прочтения» — по умолчанию нет (остаётся справочником)
    delete_after_read: bool = False
    #: Э9: картинки тела вместе с созданием: файлы должны лежать на сервере до
    #: первой доставки, иначе касса получит метку без картинки (§4.6, §7.4)
    images: list[NotificationImageIn] = Field(default_factory=list)
    target_type: str = str(TargetType.ALL)
    store_ids: list[int] = Field(default_factory=list)
    cluster: str | None = None
    deadline_at: str | None = None
    repeat_every_min: int | None = None


class NotificationOut(BaseModel):
    """Уведомление в списке `/notifications` (§11.5)."""

    id: int
    title: str
    body: str
    #: Э9: формат тела, признак «удалять после прочтения» и картинки тела
    body_format: str = richtext.BODY_FORMAT_PLAIN
    delete_after_read: bool = False
    images: list[str] = Field(default_factory=list)
    severity: str
    #: 1.22: категория уведомления для плашки и группировки в списке (§11.5)
    category: NotificationCategoryOut | None = None
    requires_ack: bool
    target_type: str
    target_value: str | None = None
    deadline_at: str | None = None
    repeat_every_min: int | None = None
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
            body_format=str(notification.body_format or richtext.BODY_FORMAT_PLAIN),
            delete_after_read=bool(notification.delete_after_read),
            images=[image.name for image in notification.images],
            severity=str(notification.severity),
            category=(
                NotificationCategoryOut.from_model(notification.category)
                if notification.category is not None
                else None
            ),
            requires_ack=bool(notification.requires_ack),
            target_type=str(notification.target_type),
            target_value=notification.target_value,
            deadline_at=to_iso_z(notification.deadline_at),
            repeat_every_min=notification.repeat_every_min,
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
    #: Э9: вкладки «все / info / important / critical» со счётчиками (§11.5)
    severity_counts: dict[str, int] = Field(default_factory=dict)
    #: Э9: сколько сессий ждёт «Ознакомлен» по каждому уровню — те же числа,
    #: что видит шапка окна кассы (§7.4)
    unread_by_severity: dict[str, int] = Field(default_factory=dict)
    pending_receipts: int
    snooze_min: int


class NotificationSummaryOut(BaseModel):
    """Счётчики вкладок страницы `/notifications` (§11.5, Э9)."""

    total: int
    by_severity: dict[str, int] = Field(default_factory=dict)
    by_state: dict[str, int] = Field(default_factory=dict)
    unread_receipts: dict[str, int] = Field(default_factory=dict)


class NotificationImageOut(BaseModel):
    """Описание картинки: имя, тип, размер и готовый адрес для SPA (§11.5, Э9)."""

    name: str
    content_type: str
    size_bytes: int
    url: str


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
    #: 1.23: сколько касс получили отзыв (`notify_cancel`) — убрать из очереди
    revoked_jobs: int = 0
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
    context: Annotated[AppContext, Depends(get_context)],
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
        body_format=str(params["body_format"]),
        severity=str(params["severity"]),
        category_id=params["category_id"],  # type: ignore[arg-type]
        requires_ack=bool(params["requires_ack"]),
        delete_after_read=bool(params["delete_after_read"]),
        target_type=str(params["target_type"]),
        stores=stores,
        cluster=params["cluster"],  # type: ignore[arg-type]
        deadline_at=params["deadline_at"],  # type: ignore[arg-type]
        repeat_every_min=params["repeat_every_min"],  # type: ignore[arg-type]
        created_by=admin.username,
    )
    jobs = _create_jobs(session, notification, author=admin.username)
    images = _store_images(
        session,
        notification,
        payload.images,
        base_dir=context.settings.notification_images_dir,
    )
    if str(notification.body_format) == richtext.BODY_FORMAT_HTML:
        # Метка картинки без файла нарисовалась бы на кассе «сломанной»:
        # оставляем в теле только те картинки, которые реально сохранены (§7.4)
        notification.body = notification_service.keep_known_images(
            notification.body, [image.name for image in images]
        )
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
            "body_format": str(notification.body_format),
            "delete_after_read": bool(notification.delete_after_read),
            "images": len(images),
            "stores": len(stores),
        },
        ip_address=get_client_ip(request),
    )
    session.commit()
    logger.info(
        "Уведомление id=%s создано администратором %s: магазинов %s, задач %s, картинок %s",
        notification.id,
        admin.username,
        len(stores),
        jobs,
        len(images),
    )
    return NotificationCreateOut(
        notification=out,
        stores=len(stores),
        jobs=jobs,
        skipped_store_ids=skipped,
        message=(
            f"Уведомление создано и поставлено в рассылку: магазинов {len(stores)}, "
            f"задач {jobs}" + (f", картинок {len(images)}" if images else "")
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

    counters = notification_service.severity_summary(session)
    return NotificationListOut(
        items=[NotificationOut.from_model(item) for item in items],
        total=len(items),
        counts=counts,
        severity_counts=dict(counters["by_severity"]),  # type: ignore[arg-type]
        unread_by_severity=dict(counters["unread_receipts"]),  # type: ignore[arg-type]
        pending_receipts=notification_service.pending_count(session),
        snooze_min=notification_service.snooze_min(session),
    )


@router.get(
    "/notifications/summary",
    response_model=NotificationSummaryOut,
    summary="Счётчики вкладок уведомлений",
)
def notifications_summary(
    session: Annotated[Session, Depends(get_db)],
) -> NotificationSummaryOut:
    """Сколько уведомлений каждого уровня и сколько сессий ждут «Ознакомлен» (Э9, §11.5).

    Отдельная точка, а не поле списка: вкладкам нужны числа и при пустом фильтре
    поиска, а окно кассы опрашивает такие счётчики чаще, чем открывает список.
    """
    data = notification_service.severity_summary(session)
    return NotificationSummaryOut(
        total=int(data["total"]),  # type: ignore[arg-type]
        by_severity=dict(data["by_severity"]),  # type: ignore[arg-type]
        by_state=dict(data["by_state"]),  # type: ignore[arg-type]
        unread_receipts=dict(data["unread_receipts"]),  # type: ignore[arg-type]
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

    summary = notification_service.cancel_notification(session, notification)
    canceled = int(summary["canceled_jobs"])
    revoked = int(summary["revoked_jobs"])
    audit_service.record(
        session,
        action="notification_cancel",
        actor=admin.username,
        entity="notification",
        entity_id=notification.id,
        details={"canceled_jobs": canceled, "revoked_jobs": revoked},
        ip_address=get_client_ip(request),
    )
    session.commit()
    logger.info(
        "Уведомление id=%s отменено администратором %s (задач закрыто: %s, отзывов: %s)",
        notification.id,
        admin.username,
        canceled,
        revoked,
    )
    return CancelNotificationOut(
        notification_id=notification.id,
        state=notification_service.state(notification),
        canceled_jobs=canceled,
        revoked_jobs=revoked,
        message=(
            f"Рассылка остановлена; незавершённых задач закрыто: {canceled}"
            + (f", кассам отправлен отзыв: {revoked}" if revoked else "")
        ),
    )


# --- Картинки тела уведомления (Э9, §4.6) -----------------------------------


def _image_out(notification_id: int, image: NotificationImage) -> NotificationImageOut:
    """Описание картинки для SPA: адрес по тому же шаблону, что и отдача файла."""
    return NotificationImageOut(
        name=image.name,
        content_type=image.content_type,
        size_bytes=int(image.size_bytes or 0),
        url=ADMIN_IMAGE_URL_TEMPLATE.format(notification_id=notification_id, name=image.name),
    )


def _decode_image(data_base64: str) -> bytes:
    """Разобрать base64 или `data:`-URL из редактора; мусор — это `422`."""
    text = str(data_base64 or "").strip()
    if text.startswith(DATA_URL_PREFIX):
        _, _, text = text.partition(",")
    try:
        return base64.b64decode(text, validate=True)
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_422, detail="Картинка пришла не в base64") from exc


def _store_images(
    session: Session,
    notification: Notification,
    images: list[NotificationImageIn],
    *,
    base_dir: Path,
) -> list[NotificationImage]:
    """Сохранить картинки, приехавшие вместе с созданием уведомления (§11.5, Э9).

    Файлы ложатся **до** первой доставки: уведомление уходит на кассу уже с
    готовыми картинками, и метка `magaz-image:<имя>` в теле всегда имеет файл
    (§4.6, §7.4). Мусор в base64, не-картинка и превышение лимитов — это `422`,
    а не «уведомление без картинок».
    """
    if not images:
        return []
    items = [(item.name.strip(), _decode_image(item.data_base64)) for item in images]
    try:
        return notification_service.add_images(
            session, notification, items=items, base_dir=base_dir
        )
    except notification_service.NotificationValidationError as exc:
        raise HTTPException(status_code=HTTP_422, detail=str(exc)) from exc


@router.post(
    "/notifications/{notification_id}/images",
    response_model=NotificationImageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить картинку в тело уведомления",
)
def upload_notification_image(
    notification_id: int,
    payload: NotificationImageIn,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    context: Annotated[AppContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db)],
    request: Request,
) -> NotificationImageOut:
    """Принять картинку из редактора и положить файл рядом с БД (Э9, §4.6).

    Тело уведомления остаётся текстом: в нём стоит метка `magaz-image:<имя>`, а
    адрес подставляет потребитель — SPA берёт этот эндпоинт, касса скачивает
    картинку через агента в свой кэш (§7.4).
    """
    notification = _get(session, notification_id)
    data = _decode_image(payload.data_base64)
    try:
        image = notification_service.add_image(
            session,
            notification,
            name=payload.name,
            data=data,
            base_dir=context.settings.notification_images_dir,
            created_by=admin.username,
        )
    except notification_service.NotificationValidationError as exc:
        raise HTTPException(status_code=HTTP_422, detail=str(exc)) from exc

    audit_service.record(
        session,
        action="notification_image_add",
        actor=admin.username,
        entity="notification",
        entity_id=notification.id,
        details={"name": image.name, "bytes": image.size_bytes},
        ip_address=get_client_ip(request),
    )
    session.commit()
    return _image_out(notification.id, image)


@router.get(
    "/notifications/{notification_id}/images/{name}",
    summary="Картинка тела уведомления",
)
def get_notification_image(
    notification_id: int,
    name: str,
    context: Annotated[AppContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db)],
) -> Response:
    """Отдать файл картинки SPA (Э9).

    Имя проверяется (`shared.richtext.is_safe_image_name`): выйти из каталога
    уведомления нельзя, а картинки, которой нет в БД, не существует и на диске —
    файл не отдаётся по одному лишь совпадению имени.
    """
    notification = _get(session, notification_id)
    image = next((item for item in notification.images if item.name == name), None)
    if image is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Картинка {name!r} у уведомления id={notification_id} не найдена",
        )
    path = notification_service.image_file_path(
        context.settings.notification_images_dir, notification.id, name
    )
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Файл картинки не найден: восстановите его из резервной копии",
        )
    return Response(
        content=notification_service.image_payload(image, path.read_bytes()),
        media_type=image.content_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


# --- Удаление (Э10) ---------------------------------------------------------


class NotificationDeleteOut(BaseModel):
    """Ответ `DELETE /notifications/{id}`: что удалено вместе с уведомлением (§5.2.2)."""

    notification_id: int
    canceled_jobs: int = 0
    #: 1.23: сколько касс получили отзыв (`notify_cancel`) — убрать из очереди
    revoked_jobs: int = 0
    images_removed: int = 0
    message: str


class NotificationImageDeleteOut(BaseModel):
    """Ответ `DELETE /notifications/{id}/images/{name}`: тело уже без метки (§11.5)."""

    notification_id: int
    name: str
    body: str
    images: list[str] = Field(default_factory=list)


@router.delete(
    "/notifications/{notification_id}",
    response_model=NotificationDeleteOut,
    summary="Удалить уведомление",
)
def delete_notification_endpoint(
    notification_id: int,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    context: Annotated[AppContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db)],
) -> NotificationDeleteOut:
    """Удалить уведомление вместе с адресатами, отметками и картинками (§11.5, Э10).

    Незавершённые задачи рассылки закрываются: офлайн-касса не получит уведомление
    после удаления. Уже показанные окна отозвать нельзя — так же, как при отмене
    (§7.4). В журнале аудита остаётся запись `notification_delete`.
    """
    notification = _get(session, notification_id)
    summary = notification_service.delete_notification(
        session, notification, base_dir=context.settings.notification_images_dir
    )
    audit_service.record(
        session,
        action="notification_delete",
        actor=admin.username,
        entity="notification",
        entity_id=notification_id,
        details={
            "title": notification.title,
            "canceled_jobs": summary["canceled_jobs"],
            "revoked_jobs": summary["revoked_jobs"],
            "images_removed": summary["images_removed"],
        },
        ip_address=get_client_ip(request),
    )
    session.commit()
    logger.info(
        "Уведомление id=%s удалено администратором %s", notification_id, admin.username
    )
    return NotificationDeleteOut(
        notification_id=notification_id,
        canceled_jobs=summary["canceled_jobs"],
        revoked_jobs=summary["revoked_jobs"],
        images_removed=summary["images_removed"],
        message=(
            "Уведомление удалено"
            + (
                f"; незавершённых задач закрыто: {summary['canceled_jobs']}"
                if summary["canceled_jobs"]
                else ""
            )
            + (
                f"; кассам отправлен отзыв: {summary['revoked_jobs']}"
                if summary["revoked_jobs"]
                else ""
            )
            + (
                f"; картинок удалено: {summary['images_removed']}"
                if summary["images_removed"]
                else ""
            )
        ),
    )


@router.delete(
    "/notifications/{notification_id}/images/{name}",
    response_model=NotificationImageDeleteOut,
    summary="Удалить картинку тела уведомления",
)
def delete_notification_image(
    notification_id: int,
    name: str,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    context: Annotated[AppContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db)],
) -> NotificationImageDeleteOut:
    """Удалить одну картинку тела: файл, запись и метку `magaz-image:<имя>` (§4.6, Э10).

    Тело уведомления остаётся согласованным: метка удалённой картинки уходит из
    разметки, поэтому ни предпросмотр, ни касса больше её не ждут (§7.4).
    """
    notification = _get(session, notification_id)
    try:
        body = notification_service.delete_image(
            session, notification, name=name, base_dir=context.settings.notification_images_dir
        )
    except notification_service.NotificationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    audit_service.record(
        session,
        action="notification_image_delete",
        actor=admin.username,
        entity="notification",
        entity_id=notification.id,
        details={"name": name},
        ip_address=get_client_ip(request),
    )
    session.commit()
    return NotificationImageDeleteOut(
        notification_id=notification.id,
        name=name,
        body=body,
        images=[str(image.name) for image in notification.images],
    )


# --- Категории уведомлений (1.22, §11.5) ------------------------------------


class NotificationCategoryDeleteOut(BaseModel):
    """Ответ мягкого удаления категории (§11.5)."""

    category_id: int
    message: str


def _category_or_404(session: Session, category_id: int) -> NotificationCategory:
    """Категория по id или `404` (§11.5)."""
    try:
        return notification_service.get_category(session, category_id)
    except notification_service.NotificationCategoryNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@router.get(
    "/notification-categories",
    response_model=list[NotificationCategoryOut],
    summary="Список категорий уведомлений",
)
def list_notification_categories(
    session: Annotated[Session, Depends(get_db)],
    include_inactive: Annotated[bool, Query()] = False,
) -> list[NotificationCategoryOut]:
    """Категории для формы и справочника (§11.5, спека 1.22).

    По умолчанию отдаются только активные: именно их предлагает форма создания
    уведомления. `include_inactive=true` — для раздела «Категории», где админ
    видит и отключённые (§11.5).
    """
    return [
        NotificationCategoryOut.from_model(category)
        for category in notification_service.list_categories(
            session, include_inactive=include_inactive
        )
    ]


@router.post(
    "/notification-categories",
    response_model=NotificationCategoryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Создать категорию уведомлений",
)
def create_notification_category(
    payload: NotificationCategoryIn,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
    request: Request,
) -> NotificationCategoryOut:
    """Завести категорию («Продажи», «Закупки») (§11.5, спека 1.22).

    Дубль имени — `409`: админ видит, что категория уже есть, а не получает
    вторую с тем же названием. Некорректный цвет — `422`.
    """
    try:
        category = notification_service.create_category(
            session,
            name=payload.name,
            color=payload.color,
            sort_order=payload.sort_order,
            created_by=admin.username,
        )
    except notification_service.NotificationCategoryValidationError as exc:
        raise _category_error(exc) from exc
    audit_service.record(
        session,
        action="notification_category_create",
        actor=admin.username,
        entity="notification_category",
        entity_id=category.id,
        details={"name": category.name, "color": category.color},
        ip_address=get_client_ip(request),
    )
    session.commit()
    return NotificationCategoryOut.from_model(category)


@router.put(
    "/notification-categories/{category_id}",
    response_model=NotificationCategoryOut,
    summary="Изменить категорию уведомлений",
)
def update_notification_category(
    category_id: int,
    payload: NotificationCategoryIn,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
    request: Request,
) -> NotificationCategoryOut:
    """Переименовать категорию, сменить цвет, порядок или включить обратно (§11.5)."""
    category = _category_or_404(session, category_id)
    try:
        notification_service.update_category(
            session,
            category,
            name=payload.name,
            color=payload.color,
            sort_order=payload.sort_order,
            is_active=payload.is_active,
        )
    except notification_service.NotificationCategoryValidationError as exc:
        raise _category_error(exc) from exc
    audit_service.record(
        session,
        action="notification_category_update",
        actor=admin.username,
        entity="notification_category",
        entity_id=category.id,
        details={"name": category.name, "is_active": bool(category.is_active)},
        ip_address=get_client_ip(request),
    )
    session.commit()
    return NotificationCategoryOut.from_model(category)


@router.delete(
    "/notification-categories/{category_id}",
    response_model=NotificationCategoryDeleteOut,
    summary="Отключить категорию уведомлений",
)
def delete_notification_category(
    category_id: int,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
    request: Request,
) -> NotificationCategoryDeleteOut:
    """Мягко отключить категорию: `is_active=false` (§11.5, спека 1.22).

    Жёсткого удаления нет: у уже разосланных уведомлений категория — часть
    истории и отчёта. Отключённая категория пропадает из выбора, но остаётся у
    старых уведомлений.
    """
    category = _category_or_404(session, category_id)
    name = str(category.name)
    notification_service.delete_category(session, category)
    audit_service.record(
        session,
        action="notification_category_delete",
        actor=admin.username,
        entity="notification_category",
        entity_id=category_id,
        details={"name": name},
        ip_address=get_client_ip(request),
    )
    session.commit()
    return NotificationCategoryDeleteOut(
        category_id=category_id,
        message=f"Категория «{name}» отключена: у уже созданных уведомлений она сохранится",
    )


def _category_error(exc: Exception) -> HTTPException:
    """Ошибка категории: дубль имени — `409`, остальное — `422` (§11.5)."""
    text = str(exc)
    if "уже есть" in text:
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=text)
    return HTTPException(status_code=HTTP_422, detail=text)

