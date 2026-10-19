"""Уведомления пользователям магазинов (§4.6–4.8, ФН3, §11.5) — сервисный слой.

Модуль отвечает за весь путь уведомления на серверной стороне:

* **адресация** (`all` / список магазинов / кластер) и создание строк
  `notification_targets` — знаменателя «Y» (§4.7);
* **доставка**: рассылка ставится задачами `notify` по магазинам, поэтому
  выключенная касса не теряет уведомление — оно ждёт в `commands` и уходит,
  как только магазин появился в сети (§11.4, FR-16);
* **приём факта доставки** от агента: `delivered_at` и `sessions_count`;
* **числитель «X»**: `notification_receipts` по сессиям Windows (П-6 — учётная
  запись, а не сотрудник). Строки `pending` создаются по составу сессий,
  который докладывает агент, поэтому отчёт честно показывает «продавец2 —
  ожидание» (§4.8);
* **«Отложить» (П-2)**: статус `snoozed` остаётся **непрочитанным**, хранит
  `snooze_count` и `snooze_until`; интервал возврата — настройка сервера
  `notification_snooze_min` (§4.9), на кассе её не правят;
* **отмена** (`is_active=false`) с закрытием ещё не отправленных задач;
* **отчёт «X из Y»** по магазинам и сессиям с выгрузкой CSV (§11.5).

Пароли и любые секреты через этот модуль не проходят: уведомление — это
заголовок, текст и адресация.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from server.db.models import (
    Command,
    Notification,
    NotificationCategory,
    NotificationImage,
    NotificationReceipt,
    NotificationTarget,
    Store,
)
from server.services import command_service, settings_service
from shared import richtext
from shared.enums import (
    CommandKind,
    CommandStatus,
    NotificationSeverity,
    NotificationStatus,
    ReceiptStatus,
    TargetType,
)
from shared.schemas import (
    NotificationCategoryRef,
    NotifyRequest,
    ReceiptBatch,
    ReceiptItem,
)
from shared.timeutil import parse_iso_z, to_iso_z, utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "CATEGORY_COLORS",
    "CATEGORY_NAME_MAX_LENGTH",
    "CSV_HEADER",
    "DEFAULT_CATEGORY_COLOR",
    "DEFAULT_LIST_LIMIT",
    "IMAGES_MAX_PER_NOTIFICATION",
    "IMAGE_CONTENT_TYPES",
    "IMAGE_MAX_BYTES",
    "OPEN_RECEIPT_STATUSES",
    "RECEIPT_STATUSES",
    "SEVERITIES",
    "SNOOZE_SETTING",
    "STATUS_ALL",
    "STATUS_FILTERS",
    "STATUS_LABELS",
    "TARGET_TYPES",
    "NotificationCategoryNotFoundError",
    "NotificationCategoryValidationError",
    "NotificationNotFoundError",
    "NotificationValidationError",
    "add_image",
    "add_images",
    "apply_receipts",
    "cancel_notification",
    "category_ref",
    "create_category",
    "create_notification",
    "delete_category",
    "delete_image",
    "delete_notification",
    "detect_image_content_type",
    "get_category",
    "get_notification",
    "image_directory",
    "image_file_path",
    "image_payload",
    "keep_known_images",
    "list_categories",
    "list_notifications",
    "mark_delivered",
    "normalize_params",
    "pending_count",
    "progress",
    "push_payload",
    "report",
    "report_csv",
    "severity_summary",
    "snooze_min",
    "state",
    "target_stores",
    "update_category",
]

#: §4.9: через сколько минут возвращается отложенное уведомление (П-2)
SNOOZE_SETTING = "notification_snooze_min"
DEFAULT_SNOOZE_MIN = 15
#: 1.22: палитра плашек категорий (§11.5) — тёмная тема кассы рассчитана на эти цвета
CATEGORY_COLORS: tuple[str, ...] = (
    "#64748b",  # серый — «без акцента»
    "#2563eb",  # синий
    "#0ea5e9",  # голубой
    "#10b981",  # зелёный
    "#f59e0b",  # янтарный
    "#ef4444",  # красный
    "#8b5cf6",  # фиолетовый
    "#ec4899",  # розовый
)
#: Цвет категории по умолчанию: нейтральный, читается и на тёмном, и на светлом
DEFAULT_CATEGORY_COLOR = CATEGORY_COLORS[0]
#: Имя категории — как колонка `notification_categories.name` (§4.6)
CATEGORY_NAME_MAX_LENGTH = 100
#: §11.5: сколько уведомлений отдаём в список по умолчанию
DEFAULT_LIST_LIMIT = 200
#: Максимальная длина заголовка — как колонка `notifications.title` (§4.6)
TITLE_MAX_LENGTH = 200
#: Ограничение текста: тело уведомления — короткое сообщение, а не документ
BODY_MAX_LENGTH = 4000
#: Э9: у тела с разметкой лимит выше — HTML-теги «съедают» символы, а картинки
#: приходят метками `magaz-image:<имя>`, которые тоже занимают место (§4.6)
BODY_MAX_LENGTH_HTML = 20000
#: Э9: картинки тела — что и сколько принимаем (§4.6)
IMAGE_MAX_BYTES = 2 * 1024 * 1024
IMAGES_MAX_PER_NOTIFICATION = 10
IMAGE_CONTENT_TYPES: tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
)
#: §4.6/§4.8: допустимые значения
SEVERITIES: tuple[str, ...] = tuple(item.value for item in NotificationSeverity)
TARGET_TYPES: tuple[str, ...] = tuple(item.value for item in TargetType)
RECEIPT_STATUSES: tuple[str, ...] = tuple(item.value for item in ReceiptStatus)
#: Статусы рассылки в UI (§11.5) и фильтр «все»
STATUS_ALL = "all"
STATUS_FILTERS: tuple[str, ...] = (STATUS_ALL, *(item.value for item in NotificationStatus))
#: Строки, которые ещё не закрыты: по ним считается плитка «Ждут ознакомления»
OPEN_RECEIPT_STATUSES: tuple[str, ...] = (
    str(ReceiptStatus.PENDING),
    str(ReceiptStatus.SHOWN),
    str(ReceiptStatus.SNOOZED),
)


class NotificationValidationError(ValueError):
    """Некорректные параметры уведомления — API отвечает `422` (§5.2.2)."""


class NotificationNotFoundError(LookupError):
    """Уведомление не найдено — API отвечает `404` (§5.2.2)."""


# --- Настройки и разбор параметров ------------------------------------------


def snooze_min(session: Session) -> int:
    """Через сколько минут снова всплывает отложенное (§4.9, П-2)."""
    value = settings_service.get_int(session, SNOOZE_SETTING)
    return max(1, int(value or DEFAULT_SNOOZE_MIN))


def _clean(value: object, *, field: str, max_length: int) -> str:
    """Обрезать и проверить обязательный текст (заголовок, тело)."""
    text = str(value or "").strip()
    if not text:
        raise NotificationValidationError(f"Поле {field} не может быть пустым")
    if len(text) > max_length:
        raise NotificationValidationError(
            f"Поле {field} длиннее {max_length} символов"
        )
    return text


def _clean_severity(value: object) -> str:
    """Уровень важности: `info` / `important` / `critical` (§4.6)."""
    severity = str(value or NotificationSeverity.INFO).strip().lower()
    if severity not in SEVERITIES:
        raise NotificationValidationError(
            "Недопустимый уровень важности: ожидается info/important/critical"
        )
    return severity


def _clean_target_type(value: object) -> str:
    """Тип адресации: `all` / `stores` / `cluster` (§4.6)."""
    target_type = str(value or TargetType.ALL).strip().lower()
    if target_type not in TARGET_TYPES:
        raise NotificationValidationError(
            "Недопустимая адресация: ожидается all/stores/cluster"
        )
    return target_type


def _clean_repeat(value: object) -> int | None:
    """Интервал повтора показа в минутах (`critical`, §4.6)."""
    if value in (None, "", 0, "0"):
        return None
    try:
        minutes = int(value)
    except (TypeError, ValueError) as exc:
        raise NotificationValidationError(
            "Интервал повтора должен быть целым числом минут"
        ) from exc
    if minutes < 1 or minutes > 24 * 60:
        raise NotificationValidationError(
            "Интервал повтора — от 1 минуты до 1440 минут"
        )
    return minutes


def _clean_category_id(value: object) -> int | None:
    """Разобрать id категории уведомления (1.22, §4.6); `None` — без категории."""
    if value in (None, "", 0, "0"):
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise NotificationValidationError("Некорректный id категории уведомления") from exc
    if number <= 0:
        return None
    return number


def _clean_store_ids(value: object) -> list[int]:
    """Разобрать список id магазинов: список, строка «1,2» или «1;2» (§11.4)."""
    if value in (None, "", []):
        return []
    if isinstance(value, (list, tuple, set)):
        items: list[object] = list(value)
    else:
        items = str(value).replace(";", ",").split(",")
    ids: list[int] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        try:
            number = int(text)
        except ValueError as exc:
            raise NotificationValidationError(f"Некорректный id магазина: {text!r}") from exc
        if number > 0 and number not in ids:
            ids.append(number)
    return ids


def _clean_body_format(value: object) -> str:
    """Формат тела: `plain` (по умолчанию) или `html` из редактора (§4.6, Э9)."""
    try:
        return richtext.normalize_body_format(value)
    except ValueError as exc:
        raise NotificationValidationError(str(exc)) from exc


def _clean_body(value: object, *, body_format: str) -> str:
    """Тело уведомления: у HTML — белый список тегов, у текста — как было (§4.6).

    Санитайз живёт здесь, а не в API: тело уходит в двух направлениях — в SPA
    (предпросмотр и отчёт) и на кассу (§7.4), поэтому чистить его должен
    единственный вход — создание уведомления.
    """
    is_html = body_format == richtext.BODY_FORMAT_HTML
    limit = BODY_MAX_LENGTH_HTML if is_html else BODY_MAX_LENGTH
    text = _clean(value, field="body", max_length=limit)
    if not is_html:
        return text
    cleaned = richtext.sanitize_html(text)
    if not richtext.html_to_text(cleaned):
        raise NotificationValidationError("В теле нет текста: одна разметка без содержимого")
    return cleaned


def normalize_params(raw: Mapping[str, object] | None) -> dict[str, object]:
    """Проверить и нормализовать тело создания уведомления (§11.5).

    Единая точка разбора для страницы `/notifications` и для действия
    `notify` в `/actions` / `/bulk/actions` (§5.2.2): правила адресации и
    уровней важности не должны расходиться между двумя входами.
    """
    data = dict(raw or {})
    store_ids = _clean_store_ids(data.get("store_ids"))
    target_type = _clean_target_type(data.get("target_type"))
    body_format = _clean_body_format(data.get("body_format"))
    cluster = str(data.get("cluster") or "").strip() or None
    if target_type == str(TargetType.STORES) and not store_ids:
        raise NotificationValidationError(
            "Адресация «магазины» требует списка id (store_ids)"
        )
    if target_type == str(TargetType.CLUSTER) and cluster is None:
        raise NotificationValidationError(
            "Адресация «кластер» требует имени кластера (cluster)"
        )
    deadline = data.get("deadline_at")
    return {
        "title": _clean(data.get("title"), field="title", max_length=TITLE_MAX_LENGTH),
        "body": _clean_body(data.get("body"), body_format=body_format),
        "body_format": body_format,
        "severity": _clean_severity(data.get("severity")),
        "requires_ack": bool(data.get("requires_ack")),
        # Э9: «удалять после прочтения» — по умолчанию нет: прочитанное остаётся
        # справочником на кассе (§4.6, §7.4)
        "delete_after_read": bool(data.get("delete_after_read", False)),
        "target_type": target_type,
        "category_id": _clean_category_id(data.get("category_id")),
        "repeat_every_min": _clean_repeat(data.get("repeat_every_min")),
        "deadline_at": parse_iso_z(deadline) if deadline else None,
        "store_ids": store_ids,
        "cluster": cluster,
    }


def target_stores(
    session: Session,
    *,
    target_type: str = str(TargetType.ALL),
    store_ids: Sequence[int] | None = None,
    cluster: str | None = None,
) -> list[Store]:
    """Выбрать магазины-получатели (§4.6, §11.4).

    Отключённые магазины (`is_active=false`) в выборку не попадают: их агенты
    остановлены, а уведомление «повисло» бы навсегда. API сообщает админу,
    какие магазины пропущены.
    """
    stmt = select(Store).where(Store.is_active.is_(True)).order_by(Store.name)
    kind = _clean_target_type(target_type)
    if kind == str(TargetType.STORES):
        ids = [int(item) for item in (store_ids or [])]
        if not ids:
            raise NotificationValidationError(
                "Адресация «магазины» требует списка id (store_ids)"
            )
        stmt = stmt.where(Store.id.in_(ids))
    elif kind == str(TargetType.CLUSTER):
        name = (cluster or "").strip()
        if not name:
            raise NotificationValidationError(
                "Адресация «кластер» требует имени кластера (cluster)"
            )
        stmt = stmt.where(Store.cluster_name == name)
    return list(session.scalars(stmt).all())


def _target_value(kind: str, stores: Sequence[Store], cluster: str | None) -> str | None:
    """`notifications.target_value` (§4.6): id списком или имя кластера."""
    if kind == str(TargetType.STORES):
        return json.dumps(sorted(store.id for store in stores))
    if kind == str(TargetType.CLUSTER):
        return cluster
    return None



def create_notification(  # noqa: PLR0913 - параметры уведомления §4.6
    session: Session,
    *,
    title: str,
    body: str,
    body_format: str = richtext.BODY_FORMAT_PLAIN,
    severity: str = str(NotificationSeverity.INFO),
    requires_ack: bool = False,
    delete_after_read: bool = False,
    target_type: str = str(TargetType.ALL),
    stores: Sequence[Store] = (),
    cluster: str | None = None,
    deadline_at: datetime | None = None,
    repeat_every_min: int | None = None,
    category_id: int | None = None,
    created_by: str | None = None,
) -> Notification:
    """Создать уведомление и строки `notification_targets` (§4.6–4.7).

    Задачи `notify` создаёт вызывающая сторона (по одной на магазин, §11.4):
    так выключенная касса получает уведомление позже, а не теряет его.
    """
    kind = _clean_target_type(target_type)
    format_kind = _clean_body_format(body_format)
    notification = Notification(
        title=_clean(title, field="title", max_length=TITLE_MAX_LENGTH),
        body=_clean_body(body, body_format=format_kind),
        body_format=format_kind,
        severity=_clean_severity(severity),
        requires_ack=bool(requires_ack),
        delete_after_read=bool(delete_after_read),
        target_type=kind,
        target_value=_target_value(kind, stores, cluster),
        deadline_at=deadline_at,
        repeat_every_min=_clean_repeat(repeat_every_min),
        category_id=_clean_category_id(category_id),
        is_active=True,
        created_by=created_by,
    )
    session.add(notification)
    session.flush()

    for store in stores:
        session.add(
            NotificationTarget(notification_id=notification.id, store_id=store.id)
        )
    session.flush()

    logger.info(
        "Уведомление id=%s создано: уровень %s, магазинов %s, автор %s",
        notification.id,
        notification.severity,
        len(stores),
        created_by or "—",
    )
    return notification


def _load_options() -> tuple:
    """Загрузка целей, магазинов, подтверждений и картинок ради отчёта (§11.5)."""
    return (
        selectinload(Notification.targets).selectinload(NotificationTarget.store),
        selectinload(Notification.targets).selectinload(NotificationTarget.receipts),
        # Э9: имена картинок нужны и в списке (`NotificationOut.images`), и в
        # полезной нагрузке для кассы (`push_payload`)
        selectinload(Notification.images),
    )


def get_notification(session: Session, notification_id: int) -> Notification:
    """Уведомление по id вместе с целями и подтверждениями (§5.2.2)."""
    notification = session.scalars(
        select(Notification)
        .where(Notification.id == notification_id)
        .options(*_load_options())
    ).first()
    if notification is None:
        raise NotificationNotFoundError(f"Уведомление id={notification_id} не найдено")
    return notification



def list_notifications(
    session: Session,
    *,
    status: str | None = None,
    severity: str | None = None,
    search: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[Notification]:
    """Список уведомлений с фильтрами страницы `/notifications` (§11.5).

    Фильтр по статусу (в работе / завершено / отменено) считается по факту —
    по прогрессу и дедлайну, — поэтому «завершено» не хранится отдельной
    колонкой и не может разойтись с отчётом.
    """
    stmt = (
        select(Notification)
        .options(*_load_options())
        .order_by(Notification.id.desc())
        .limit(max(1, int(limit)))
    )
    severity_filter = (severity or "").strip().lower()
    if severity_filter and severity_filter != STATUS_ALL:
        if severity_filter not in SEVERITIES:
            raise NotificationValidationError("Недопустимый уровень важности в фильтре")
        stmt = stmt.where(Notification.severity == severity_filter)

    search_text = (search or "").strip()
    if search_text:
        pattern = f"%{search_text}%"
        stmt = stmt.where(
            Notification.title.ilike(pattern) | Notification.body.ilike(pattern)
        )

    rows = list(session.scalars(stmt).all())
    status_filter = (status or "").strip().lower()
    if status_filter and status_filter != STATUS_ALL:
        if status_filter not in STATUS_FILTERS:
            raise NotificationValidationError("Недопустимый статус в фильтре")
        rows = [item for item in rows if state(item) == status_filter]
    return rows


def progress(notification: Notification) -> dict[str, int]:
    """Прогресс «ознакомлены X из Y» (§4.8, §11.5).

    Считается по строкам `notification_receipts`: числитель — только явное
    «Ознакомлен», поэтому отложенное (`snoozed`) числитель не увеличивает
    (П-2), а `shown` честно виден отдельной цифрой.
    """
    receipts = [item for target in notification.targets for item in target.receipts]
    acknowledged = sum(1 for item in receipts if item.is_acknowledged)
    shown = sum(1 for item in receipts if str(item.status) == str(ReceiptStatus.SHOWN))
    snoozed = sum(1 for item in receipts if str(item.status) == str(ReceiptStatus.SNOOZED))
    expired = sum(1 for item in receipts if str(item.status) == str(ReceiptStatus.EXPIRED))
    total = len(receipts)
    return {
        "stores": len(notification.targets),
        "stores_delivered": sum(
            1 for target in notification.targets if target.delivered_at is not None
        ),
        "sessions": sum(int(target.sessions_count or 0) for target in notification.targets),
        "receipts": total,
        "acknowledged": acknowledged,
        "shown": shown,
        "snoozed": snoozed,
        "expired": expired,
        "pending": max(0, total - acknowledged - shown - snoozed - expired),
        "percent": int(round(100 * acknowledged / total)) if total else 0,
    }


def state(notification: Notification, *, now: datetime | None = None) -> str:
    """Статус рассылки для списка `/notifications` (§11.5).

    * `canceled` — админ нажал «отменить» (`is_active=false`);
    * `done` — все сессии ознакомлены, дедлайн прошёл или, для уведомления без
      требования «ознакомлен», все сессии его увидели;
    * `active` — работа продолжается.
    """
    if not notification.is_active:
        return str(NotificationStatus.CANCELED)
    moment = now or utcnow()
    totals = progress(notification)
    if totals["receipts"] and totals["acknowledged"] >= totals["receipts"]:
        return str(NotificationStatus.DONE)
    if notification.deadline_at is not None and notification.deadline_at <= moment:
        return str(NotificationStatus.DONE)
    if (
        not notification.requires_ack
        and totals["receipts"]
        and totals["shown"] >= totals["receipts"]
    ):
        return str(NotificationStatus.DONE)
    return str(NotificationStatus.ACTIVE)


def pending_count(session: Session, *, now: datetime | None = None) -> int:
    """Сколько сессий ждут ознакомления — плитка главной (§11.1).

    Учитываются только доставленные уведомления, которые ещё в работе: дедлайн
    прошёл — строка уже «просрочена» и в ожидание не попадает.
    """
    moment = now or utcnow()
    stmt = (
        select(func.count())
        .select_from(NotificationReceipt)
        .join(NotificationTarget, NotificationTarget.id == NotificationReceipt.target_id)
        .join(Notification, Notification.id == NotificationTarget.notification_id)
        .where(NotificationReceipt.status.in_(OPEN_RECEIPT_STATUSES))
        .where(Notification.is_active.is_(True))
        .where(NotificationTarget.delivered_at.is_not(None))
        .where(
            (Notification.deadline_at.is_(None)) | (Notification.deadline_at > moment)
        )
    )
    return int(session.scalar(stmt) or 0)


def severity_summary(session: Session) -> dict[str, object]:
    """Счётчики вкладок страницы `/notifications` (§11.5, Э9).

    Админу нужны вкладки «все / info / important / critical» со счётчиками, а не
    один общий список: уровни считаются прямо в SQL, состояния — через
    `state()`, потому что «в работе / завершено / отменено» вычисляются по факту
    (прогресс и дедлайн), а не хранятся колонкой. `unread_receipts` — сколько
    сессий ещё ждёт «Ознакомлен» по каждому уровню: это то же число, что видит
    окно кассы в шапке (§7.4).
    """
    by_severity = dict.fromkeys(SEVERITIES, 0)
    for severity, count in session.execute(
        select(Notification.severity, func.count()).group_by(Notification.severity)
    ).all():
        by_severity[str(severity)] = int(count)

    by_state = dict.fromkeys((item for item in STATUS_FILTERS if item != STATUS_ALL), 0)
    for item in session.scalars(select(Notification).options(*_load_options())).all():
        kind = state(item)
        by_state[kind] = by_state.get(kind, 0) + 1

    unread = dict.fromkeys(SEVERITIES, 0)
    rows = session.execute(
        select(Notification.severity, func.count())
        .select_from(NotificationReceipt)
        .join(NotificationTarget, NotificationTarget.id == NotificationReceipt.target_id)
        .join(Notification, Notification.id == NotificationTarget.notification_id)
        .where(NotificationReceipt.status.in_(OPEN_RECEIPT_STATUSES))
        .where(Notification.is_active.is_(True))
        .where(NotificationTarget.delivered_at.is_not(None))
        .group_by(Notification.severity)
    ).all()
    for severity, count in rows:
        unread[str(severity)] = int(count)

    return {
        "total": sum(by_severity.values()),
        "by_severity": by_severity,
        "by_state": by_state,
        "unread_receipts": unread,
    }


# --- Доставка на кассу и приём подтверждений --------------------------------


def push_payload(session: Session, store: Store, notification: Notification) -> NotifyRequest:
    """Собрать тело `POST /notify` для кассы (§5.1.2, §7.4).

    Интервал возврата отложенного (`snooze_min`) задаёт **сервер** (П-2):
    касса получает его вместе с уведомлением и не хранит собственных
    настроек расписания.

    Э9: на кассу уезжают формат тела (`body_format`), список картинок
    (`images`) и признак «удалять после прочтения»: окно кассы рисует разметку
    само, картинки скачивает один раз в локальный кэш, а прочитанное либо
    оставляет справочником, либо убирает (§4.6, §7.4).

    1.22: вместе с уведомлением едет категория (`id`, `name`, `color`) — касса
    группирует список и рисует плашку, не зная ничего о каталоге категорий (§7.4).
    """
    return NotifyRequest(
        id=notification.id,
        title=notification.title,
        body=notification.body,
        body_format=str(notification.body_format or richtext.BODY_FORMAT_PLAIN),
        severity=str(notification.severity),
        category=category_ref(notification.category),
        requires_ack=bool(notification.requires_ack),
        delete_after_read=bool(notification.delete_after_read),
        images=[image.name for image in notification.images],
        deadline_at=to_iso_z(notification.deadline_at),
        repeat_every_min=notification.repeat_every_min,
        snooze_min=snooze_min(session),
        store_id=store.id,
    )


def category_ref(category: NotificationCategory | None) -> NotificationCategoryRef | None:
    """Категория для кассы: `None` — уведомление без категории (1.22, §7.4)."""
    if category is None:
        return None
    return NotificationCategoryRef(
        id=int(category.id),
        name=str(category.name),
        color=str(category.color or DEFAULT_CATEGORY_COLOR),
    )


# --- Картинки тела уведомления (§4.6, Э9) -----------------------------------


def image_directory(base_dir: Path, notification_id: int) -> Path:
    """Каталог картинок одного уведомления: `base_dir/<id уведомления>`."""
    return Path(base_dir) / str(int(notification_id))


def image_file_path(base_dir: Path, notification_id: int, name: str) -> Path:
    """Путь к файлу картинки; имя проверяется — выйти из каталога нельзя."""
    if not richtext.is_safe_image_name(name):
        raise NotificationNotFoundError(f"Картинка {name!r} не найдена")
    return image_directory(base_dir, notification_id) / name


def detect_image_content_type(data: bytes) -> str | None:
    """Тип картинки по «магическим» байтам — без PIL и без доверия к имени файла.

    Редактор админки отдаёт картинку как `data:`-URL, поэтому полагаться на
    присланный `content_type` нельзя: сохраняем только то, что действительно
    похоже на png/jpeg/gif/webp (§4.6).
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _images_of(session: Session, notification: Notification) -> list[NotificationImage]:
    """Картинки уведомления «как в БД»: видны и строки этой же транзакции (§11.5).

    Редактор присылает картинки вместе с созданием уведомления, и без перечитки
    связи лимит «10 картинок» и проверка имени не сработали бы: в памяти список
    остаётся пустым, пока транзакция не закрыта.
    """
    session.refresh(notification, ["images"])
    return list(notification.images)


def add_image(
    session: Session,
    notification: Notification,
    *,
    name: str,
    data: bytes,
    base_dir: Path,
    created_by: str | None = None,
) -> NotificationImage:
    """Принять картинку в тело уведомления: проверить, записать файл, учесть в БД.

    Файл лежит отдельно от БД (`base_dir/<id>/<имя>`), в теле — только метка
    `magaz-image:<имя>` (`shared.richtext`), поэтому тело остаётся компактным и
    переносимым, а касса скачивает картинки один раз в свой кэш (§7.4).
    """
    if not richtext.is_safe_image_name(name):
        raise NotificationValidationError(
            "Имя картинки: латинские буквы, цифры, точка, дефис и подчёркивание"
        )
    content_type = detect_image_content_type(data)
    if content_type is None or content_type not in IMAGE_CONTENT_TYPES:
        raise NotificationValidationError("Картинка должна быть PNG, JPEG, GIF или WebP")
    if len(data) > IMAGE_MAX_BYTES:
        raise NotificationValidationError(
            f"Картинка больше {IMAGE_MAX_BYTES // (1024 * 1024)} МБ"
        )
    existing = _images_of(session, notification)
    if len(existing) >= IMAGES_MAX_PER_NOTIFICATION:
        raise NotificationValidationError(
            f"К одному уведомлению можно приложить не больше "
            f"{IMAGES_MAX_PER_NOTIFICATION} картинок"
        )
    if any(image.name == name for image in existing):
        raise NotificationValidationError(f"Картинка с именем {name} уже есть")

    directory = image_directory(base_dir, notification.id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(data)

    image = NotificationImage(
        name=name,
        content_type=content_type,
        size_bytes=len(data),
        checksum=hashlib.sha256(data).hexdigest(),
        created_by=created_by,
    )
    # Связь проставляется через коллекцию, а не только внешним ключом: иначе
    # `notification.images` в этом же запросе остаётся старым (пустым), и в ответе
    # создания уведомление выглядело бы «без картинок» (§11.5, §7.4)
    notification.images.append(image)
    session.add(image)
    session.flush()
    logger.info(
        "Картинка %s добавлена к уведомлению id=%s: %s байт, тип %s",
        name,
        notification.id,
        len(data),
        content_type,
    )
    return image


def image_payload(image: NotificationImage, data: bytes) -> bytes:
    """Байты картинки для отдачи потребителю (SPA или касса)."""
    expected = int(image.size_bytes or 0)
    if expected and len(data) != expected:
        logger.warning(
            "Размер картинки %s уведомления id=%s не совпал: в БД %s, в файле %s",
            image.name,
            image.notification_id,
            expected,
            len(data),
        )
    return data


def add_images(
    session: Session,
    notification: Notification,
    *,
    items: Sequence[tuple[str, bytes]],
    base_dir: Path,
    created_by: str | None = None,
) -> list[NotificationImage]:
    """Приложить к уведомлению сразу несколько картинок (§4.6, Э9).

    Редактор админки отправляет картинки вместе с созданием уведомления: файлы
    должны лежать на сервере **до** первой доставки, иначе касса получит метку
    `magaz-image:<имя>` без файла и нарисует «сломанное изображение» (§7.4).
    """
    if len(items) > IMAGES_MAX_PER_NOTIFICATION:
        raise NotificationValidationError(
            f"К одному уведомлению можно приложить не больше "
            f"{IMAGES_MAX_PER_NOTIFICATION} картинок"
        )
    stored: list[NotificationImage] = []
    for name, data in items:
        stored.append(
            add_image(
                session,
                notification,
                name=name,
                data=data,
                base_dir=base_dir,
                created_by=created_by,
            )
        )
    return stored


def keep_known_images(body: str, names: Iterable[str]) -> str:
    """Убрать из тела метки картинок, которых нет на сервере (§4.6, Э9).

    Тело уже прошло белый список, но метка без файла доехала бы до кассы битой
    картинкой: лучше уведомление без картинки, чем со «сломанной» (§7.4).
    """
    return richtext.sanitize_html(body, allowed_images=set(names))


def mark_delivered(
    session: Session,
    store: Store,
    notification: Notification,
    *,
    sessions: int = 0,
) -> NotificationTarget | None:
    """Отметить доставку уведомления магазину (§4.7).

    `sessions_count` не обнуляется: агент мог ещё не увидеть ни одной сессии
    (никто не вошёл), и это не должно «стирать» уже известные цифры.
    """
    target = session.scalars(
        select(NotificationTarget)
        .where(NotificationTarget.notification_id == notification.id)
        .where(NotificationTarget.store_id == store.id)
    ).first()
    if target is None:
        logger.warning(
            "Уведомление id=%s не адресовано магазину id=%s — доставка не учтена",
            notification.id,
            store.id,
        )
        return None

    target.delivered_at = utcnow()
    if sessions > 0:
        target.sessions_count = int(sessions)
    session.flush()
    logger.info(
        "Уведомление id=%s доставлено магазину id=%s (сессий %s)",
        notification.id,
        store.id,
        target.sessions_count,
    )
    return target


def _find_receipt(target: NotificationTarget, session_id: str) -> NotificationReceipt | None:
    """Строка подтверждения по сессии Windows (§4.8, уникальность по паре)."""
    for receipt in target.receipts:
        if str(receipt.session_id) == str(session_id):
            return receipt
    return None


def _apply_receipt(receipt: NotificationReceipt, item: ReceiptItem, *, now: datetime) -> bool:
    """Записать одно подтверждение; `False` — запись не изменилась (§4.8).

    Главное правило П-2: `snoozed` **не** считается ознакомлением, а позднее
    `shown` из буфера агента не должно «откатывать» уже поставленное
    «Ознакомлен» — иначе отчёт врал бы в пользу админа.
    """
    status = str(item.status)
    if receipt.is_acknowledged and status != str(ReceiptStatus.ACKNOWLEDGED):
        return False

    receipt.session_user = item.session_user or receipt.session_user
    if item.shown_at and receipt.shown_at is None:
        receipt.shown_at = parse_iso_z(item.shown_at)

    if status == str(ReceiptStatus.ACKNOWLEDGED):
        receipt.status = status
        receipt.acknowledged_at = now
        receipt.ack_comment = item.comment
        receipt.snooze_until = None
        if receipt.shown_at is None:
            receipt.shown_at = now
        return True

    if status == str(ReceiptStatus.SHOWN):
        receipt.status = status
        if receipt.shown_at is None:
            receipt.shown_at = now
        return True

    if status == str(ReceiptStatus.SNOOZED):
        receipt.status = status
        receipt.snooze_count = int(receipt.snooze_count or 0) + 1
        receipt.snooze_until = parse_iso_z(item.snooze_until) or (
            now + timedelta(minutes=DEFAULT_SNOOZE_MIN)
        )
        return True

    if status == str(ReceiptStatus.EXPIRED):
        receipt.status = status
        return True

    receipt.status = status
    return True


def apply_receipts(session: Session, store: Store, batch: ReceiptBatch) -> dict[str, int]:
    """Принять пакет подтверждений от кассы (§5.2.2, §4.8).

    Пакет несёт две вещи, и обе нужны отчёту «X из Y»:

    * `sessions` — состав сессий, который TrayAgent реально видит на кассе: по
      нему создаются строки `pending`, поэтому «продавец2 — ожидание» видно
      админу, даже если второй продавец ещё ничего не нажимал;
    * `receipts` — что произошло: `shown` (тост `info`), `acknowledged`
      («Ознакомлен»), `snoozed` («Отложить») с `snooze_until`.

    Уведомления, не адресованные этому магазину, игнорируются — как и чужие
    адреса в батче оборудования: касса не может «дописать» себе получателей.
    """
    now = utcnow()
    created = updated = ignored = 0

    targets = list(
        session.scalars(
            select(NotificationTarget)
            .where(NotificationTarget.store_id == store.id)
            .options(
                selectinload(NotificationTarget.notification),
                selectinload(NotificationTarget.receipts),
            )
        ).all()
    )
    by_notification = {target.notification_id: target for target in targets}

    # 1) Состав сессий: «ждут ознакомления» по доставленным уведомлениям (§4.8)
    if batch.sessions:
        for target in targets:
            notification = target.notification
            if target.delivered_at is None or notification is None:
                continue
            if not notification.is_active:
                continue
            for ref in batch.sessions:
                if _find_receipt(target, ref.sid) is not None:
                    continue
                target.receipts.append(
                    NotificationReceipt(
                        target_id=target.id,
                        session_id=ref.sid,
                        session_user=ref.user,
                        status=str(ReceiptStatus.PENDING),
                    )
                )
                created += 1

    # 2) Явные подтверждения: то, что пользователи уже нажимали
    for item in batch.receipts:
        target = by_notification.get(int(item.notification_id))
        if target is None:
            ignored += 1
            continue
        receipt = _find_receipt(target, item.session_id)
        if receipt is None:
            receipt = NotificationReceipt(
                target_id=target.id,
                session_id=item.session_id,
                session_user=item.session_user,
                status=str(ReceiptStatus.PENDING),
            )
            target.receipts.append(receipt)
            created += 1
        if _apply_receipt(receipt, item, now=now):
            updated += 1
        else:
            ignored += 1

    session.flush()
    expired = _expire_overdue(session, store.id, now=now)
    if created or updated:
        logger.info(
            "Магазин id=%s: подтверждений создано %s, обновлено %s, пропущено %s, просрочено %s",
            store.id,
            created,
            updated,
            ignored,
            expired,
        )
    return {"created": created, "updated": updated, "ignored": ignored, "expired": expired}


def _expire_overdue(session: Session, store_id: int, *, now: datetime) -> int:
    """Закрыть `expired` то, что не ознакомлено к дедлайну (§4.8, §11.5)."""
    targets = session.scalars(
        select(NotificationTarget)
        .where(NotificationTarget.store_id == store_id)
        .where(NotificationTarget.delivered_at.is_not(None))
        .options(
            selectinload(NotificationTarget.notification),
            selectinload(NotificationTarget.receipts),
        )
    ).all()
    expired = 0
    for target in targets:
        notification = target.notification
        if notification is None or notification.deadline_at is None:
            continue
        if notification.deadline_at > now:
            continue
        for receipt in target.receipts:
            if receipt.is_acknowledged or str(receipt.status) == str(ReceiptStatus.EXPIRED):
                continue
            receipt.status = str(ReceiptStatus.EXPIRED)
            expired += 1
    if expired:
        session.flush()
        logger.info(
            "Магазин id=%s: не ознакомлены к дедлайну, отмечено просроченными: %s",
            store_id,
            expired,
        )
    return expired

    if notification.deadline_at is not None and notification.deadline_at <= moment:
        return str(NotificationStatus.DONE)
    if (
        not notification.requires_ack
        and totals["receipts"]
        and totals["shown"] >= totals["receipts"]
    ):
        return str(NotificationStatus.DONE)
    return str(NotificationStatus.ACTIVE)


# --- Отмена и отчёт ---------------------------------------------------------


def _command_notification_id(command: Command) -> int:
    """id уведомления из параметров задачи `notify` (или `0`)."""
    try:
        return int((command.params or {}).get("notification_id") or 0)
    except (TypeError, ValueError):
        return 0


def _notify_commands(
    session: Session, notification_id: int, statuses: Sequence[str]
) -> list[Command]:
    """Задачи рассылки конкретного уведомления в указанных статусах (§4.3)."""
    stmt = (
        select(Command)
        .where(Command.kind == str(CommandKind.NOTIFY))
        .where(Command.status.in_(tuple(statuses)))
        .order_by(Command.id)
    )
    return [
        command
        for command in session.scalars(stmt).all()
        if _command_notification_id(command) == int(notification_id)
    ]


def cancel_notification(session: Session, notification: Notification) -> int:
    """Отменить рассылку (§11.5) и закрыть ещё не отправленные задачи.

    Уже показанные окна отозвать нельзя: TrayAgent перестанет отдавать
    уведомление сам — `GET /notifications/pending` отдаёт только активные
    (§7.4), — а отчёт сохранит историю «кто успел ознакомиться».
    """
    notification.is_active = False
    canceled = 0
    pending = _notify_commands(
        session, notification.id, statuses=command_service.ACTIVE_STATUSES
    )
    for command in pending:
        command_service.set_status(session, command, CommandStatus.CANCELED)
        command_service.add_log(session, command, "Уведомление отменено администратором")
        canceled += 1
    session.flush()
    logger.info(
        "Уведомление id=%s отменено; незавершённых задач закрыто: %s",
        notification.id,
        canceled,
    )
    return canceled


def _receipt_row(
    receipt: NotificationReceipt, notification: Notification, *, now: datetime
) -> dict[str, object]:
    """Строка сессии в отчёте (§11.5): статус, времена и счётчик «отложено»."""
    overdue = (
        not receipt.is_acknowledged
        and notification.deadline_at is not None
        and notification.deadline_at <= now
    )
    return {
        "session_user": receipt.session_user,
        "session_id": receipt.session_id,
        "status": str(receipt.status),
        "shown_at": to_iso_z(receipt.shown_at),
        "acknowledged_at": to_iso_z(receipt.acknowledged_at),
        "ack_comment": receipt.ack_comment,
        "snooze_count": int(receipt.snooze_count or 0),
        "snooze_until": to_iso_z(receipt.snooze_until),
        "is_acknowledged": receipt.is_acknowledged,
        "is_overdue": overdue,
    }


def report(session: Session, notification: Notification) -> dict[str, object]:
    """Отчёт «X из Y»: магазины → сессии (§11.5).

    Строка на магазин создаётся даже без подтверждений: так админ видит, что
    уведомление доставлено, но на кассе ещё никто не вошёл под своей учётной
    записью, вместо «магазин пропал из отчёта».
    """
    now = utcnow()
    stores: list[dict[str, object]] = []
    for target in sorted(notification.targets, key=lambda item: item.id):
        store = target.store
        receipts = sorted(
            target.receipts, key=lambda item: (str(item.session_user), str(item.session_id))
        )
        stores.append(
            {
                "store_id": target.store_id,
                "store_name": store.name if store is not None else None,
                "cluster_name": store.cluster_name if store is not None else None,
                "store_is_active": bool(store.is_active) if store is not None else True,
                "delivered_at": to_iso_z(target.delivered_at),
                "sessions_count": int(target.sessions_count or 0),
                "total": len(receipts),
                "acknowledged": sum(1 for item in receipts if item.is_acknowledged),
                "receipts": [_receipt_row(item, notification, now=now) for item in receipts],
            }
        )
    return {
        "notification_id": notification.id,
        "state": state(notification, now=now),
        "deadline_at": to_iso_z(notification.deadline_at),
        "created_at": to_iso_z(notification.created_at),
        "totals": progress(notification),
        "stores": stores,
    }




#: Заголовок CSV-выгрузки отчёта (§11.5) — Excel-совместимый (`;` + BOM)
CSV_HEADER: tuple[str, ...] = (
    "Магазин",
    "Сессия (пользователь)",
    "Session ID",
    "Статус",
    "Показано",
    "Ознакомлен",
    "Отложено, раз",
    "Отложено до",
    "Комментарий",
    "Дедлайн",
    "Просрочено",
)

#: Человекочитаемые статусы для выгрузки (§4.8)
STATUS_LABELS: dict[str, str] = {
    str(ReceiptStatus.PENDING): "ожидание",
    str(ReceiptStatus.SHOWN): "показано",
    str(ReceiptStatus.ACKNOWLEDGED): "ознакомлен",
    str(ReceiptStatus.SNOOZED): "отложено",
    str(ReceiptStatus.EXPIRED): "просрочено",
}


def report_csv(data: Mapping[str, object]) -> str:
    """Отчёт в CSV (§11.5): `;` и BOM — Excel открывает файл без «кракозябр»."""
    deadline = str(data.get("deadline_at") or "")
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(CSV_HEADER)

    stores = data.get("stores")
    for store in stores if isinstance(stores, list) else []:
        if not isinstance(store, dict):
            continue
        receipts = store.get("receipts")
        rows = receipts if isinstance(receipts, list) else []
        if not rows:
            # Магазин, в котором ещё никто не вошёл: строка «ожидание» (§4.8)
            writer.writerow(
                [
                    store.get("store_name") or "",
                    "",
                    "",
                    STATUS_LABELS[str(ReceiptStatus.PENDING)],
                    "",
                    "",
                    "",
                    "",
                    "",
                    deadline,
                    "",
                ]
            )
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "")
            writer.writerow(
                [
                    store.get("store_name") or "",
                    row.get("session_user") or "",
                    row.get("session_id") or "",
                    STATUS_LABELS.get(status, status),
                    row.get("shown_at") or "",
                    row.get("acknowledged_at") or "",
                    row.get("snooze_count") or 0,
                    row.get("snooze_until") or "",
                    row.get("ack_comment") or "",
                    deadline,
                    "да" if row.get("is_overdue") else "",
                ]
            )
    return "\ufeff" + buffer.getvalue()


# --- Удаление (Э10) ---------------------------------------------------------


def delete_image(
    session: Session,
    notification: Notification,
    *,
    name: str,
    base_dir: Path,
) -> str:
    """Удалить картинку тела: файл, запись и метку `magaz-image:<имя>` (§11.5, Э10).

    Возвращает новое тело уведомления. Кассы, которые уведомление уже получили,
    картинку сохранят в своём локальном кэше: удаление действует на сервере и на
    то, что ещё не доставлено (§7.4) — окно уведомления рисует картинку без сети.
    """
    image = next((item for item in _images_of(session, notification) if item.name == name), None)
    if image is None:
        raise NotificationNotFoundError(
            f"Картинка {name!r} у уведомления id={notification.id} не найдена"
        )
    path = image_file_path(base_dir, notification.id, name)
    if path.is_file():
        try:
            path.unlink()
        except OSError:  # pragma: no cover - файл занят или каталог только для чтения
            logger.warning(
                "Файл картинки %s уведомления id=%s удалить не удалось", name, notification.id
            )
    if image in notification.images:
        notification.images.remove(image)
    session.delete(image)
    session.flush()

    remaining = [item.name for item in _images_of(session, notification)]
    if notification.body_format == richtext.BODY_FORMAT_HTML:
        notification.body = richtext.sanitize_html(
            notification.body, allowed_images=set(remaining)
        )
        session.flush()
    logger.info("Картинка %s удалена у уведомления id=%s", name, notification.id)
    return notification.body


def delete_notification(
    session: Session, notification: Notification, *, base_dir: Path
) -> dict[str, int]:
    """Удалить уведомление целиком: задачи, картинки, отметки, адресаты (§11.5, Э10).

    Порядов удаления: сначала закрываются незавершённые задачи рассылки (чтобы
    офлайн-касса не получила уведомление после удаления), затем снимаются файлы
    картинок, отметки «ознакомлен», адресаты и само уведомление.

    Уже показанные на кассах окна не отзываются — это то же ограничение, что и у
    отмены (§7.4): сервер не управляет тем, что уже нарисовано в сессии Windows.
    """
    canceled = 0
    for command in _notify_commands(
        session, notification.id, statuses=command_service.ACTIVE_STATUSES
    ):
        command_service.set_status(session, command, CommandStatus.CANCELED)
        command_service.add_log(session, command, "Уведомление удалено администратором")
        canceled += 1

    names = [str(image.name) for image in _images_of(session, notification)]
    for name in names:
        path = image_file_path(base_dir, notification.id, name)
        if not path.is_file():
            continue
        try:
            path.unlink()
        except OSError:  # pragma: no cover - файл занят
            logger.warning(
                "Файл картинки %s уведомления id=%s удалить не удалось", name, notification.id
            )
    directory = image_directory(base_dir, notification.id)
    if directory.is_dir():
        try:
            directory.rmdir()
        except OSError:
            logger.warning(
                "Каталог картинок уведомления id=%s остался (в нём есть чужие файлы)",
                notification.id,
            )

    targets = select(NotificationTarget.id).where(
        NotificationTarget.notification_id == notification.id
    )
    session.execute(
        delete(NotificationReceipt).where(NotificationReceipt.target_id.in_(targets))
    )
    session.execute(
        delete(NotificationTarget).where(NotificationTarget.notification_id == notification.id)
    )
    session.execute(
        delete(NotificationImage).where(NotificationImage.notification_id == notification.id)
    )
    session.delete(notification)
    session.flush()
    logger.info(
        "Уведомление id=%s удалено; задач закрыто: %s, картинок: %s",
        notification.id,
        canceled,
        len(names),
    )
    return {"canceled_jobs": canceled, "images_removed": len(names)}


# --- Категории уведомлений (1.22, §4.6, §11.5) ------------------------------


class NotificationCategoryNotFoundError(LookupError):
    """Категория не найдена: API отвечает `404` (§11.5)."""


class NotificationCategoryValidationError(ValueError):
    """Некорректные данные категории: имя пустое, дубль или неизвестный цвет."""


def _clean_category_name(value: object) -> str:
    """Имя категории: непустое, обрезанное, не длиннее колонки (§4.6)."""
    name = str(value or "").strip()
    if not name:
        raise NotificationCategoryValidationError("Имя категории не может быть пустым")
    if len(name) > CATEGORY_NAME_MAX_LENGTH:
        raise NotificationCategoryValidationError(
            f"Имя категории длиннее {CATEGORY_NAME_MAX_LENGTH} символов"
        )
    return name


def _clean_category_color(value: object) -> str:
    """Цвет плашки: `#RRGGBB` из палитры (§11.5).

    Свободный цвет не принимаем: плашки рисуются на тёмном фоне кассы и на
    светлом дашборде, поэтому палитра ограничена читаемыми оттенками.
    """
    color = str(value or "").strip().lower()
    if not color:
        return DEFAULT_CATEGORY_COLOR
    if color not in CATEGORY_COLORS:
        raise NotificationCategoryValidationError(
            "Цвет должен быть из палитры: " + ", ".join(CATEGORY_COLORS)
        )
    return color


def _clean_sort_order(value: object) -> int:
    """Порядок в списках: целое, `0` — «как заведено»."""
    if value in (None, ""):
        return 0
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise NotificationCategoryValidationError("Порядок должен быть целым числом") from exc


def list_categories(
    session: Session, *, include_inactive: bool = False
) -> list[NotificationCategory]:
    """Категории в порядке админа: сортировка по `sort_order`, затем по имени (1.22).

    `include_inactive` нужен админке: отключённая категория пропадает из выбора
    при создании уведомления, но её видно в справочнике категорий.
    """
    stmt = select(NotificationCategory).order_by(
        NotificationCategory.sort_order, NotificationCategory.name
    )
    if not include_inactive:
        stmt = stmt.where(NotificationCategory.is_active.is_(True))
    return list(session.scalars(stmt).all())


def get_category(session: Session, category_id: int) -> NotificationCategory:
    """Категория по id или `NotificationCategoryNotFoundError` (§11.5)."""
    category = session.get(NotificationCategory, int(category_id))
    if category is None:
        raise NotificationCategoryNotFoundError(f"Категория id={category_id} не найдена")
    return category


def _category_name_taken(
    session: Session, name: str, *, exclude_id: int | None = None
) -> bool:
    """Занято ли имя категории — сравнение без учёта регистра (§4.6).

    Сравниваем в Python, а не `LOWER()` в SQL: SQLite не умеет `lower()` для
    кириллицы, поэтому «Закупки» и «закупки» там считались бы разными именами.
    """
    target = name.strip().casefold()
    stmt = select(NotificationCategory.id, NotificationCategory.name)
    if exclude_id is not None:
        stmt = stmt.where(NotificationCategory.id != int(exclude_id))
    for row in session.execute(stmt).all():
        if str(row.name).strip().casefold() == target:
            return True
    return False


def create_category(  # noqa: PLR0913 - поля категории §4.6
    session: Session,
    *,
    name: str,
    color: str = DEFAULT_CATEGORY_COLOR,
    sort_order: int = 0,
    created_by: str | None = None,
) -> NotificationCategory:
    """Завести категорию уведомлений (1.22, §11.5).

    Имя уникально (`uq_notification_categories_name`) — админка ловит дубль по
    `NotificationCategoryValidationError` и отвечает `409`.
    """
    clean_name = _clean_category_name(name)
    if _category_name_taken(session, clean_name):
        raise NotificationCategoryValidationError(f"Категория «{clean_name}» уже есть")
    category = NotificationCategory(
        name=clean_name,
        color=_clean_category_color(color),
        sort_order=_clean_sort_order(sort_order),
        is_active=True,
        created_by=created_by,
    )
    session.add(category)
    session.flush()
    logger.info("Категория уведомлений id=%s «%s» создана", category.id, category.name)
    return category


def update_category(  # noqa: PLR0913 - поля категории §4.6
    session: Session,
    category: NotificationCategory,
    *,
    name: str | None = None,
    color: str | None = None,
    sort_order: int | None = None,
    is_active: bool | None = None,
) -> NotificationCategory:
    """Изменить категорию: имя, цвет, порядок и признак активности (§11.5).

    Переданные `None`-поля не меняются: так обновление одного цвета не затирает
    имя и порядок, которые админка не отправляла.
    """
    if name is not None:
        clean_name = _clean_category_name(name)
        if _category_name_taken(session, clean_name, exclude_id=category.id):
            raise NotificationCategoryValidationError(f"Категория «{clean_name}» уже есть")
        category.name = clean_name
    if color is not None:
        category.color = _clean_category_color(color)
    if sort_order is not None:
        category.sort_order = _clean_sort_order(sort_order)
    if is_active is not None:
        category.is_active = bool(is_active)
    session.flush()
    logger.info("Категория уведомлений id=%s обновлена", category.id)
    return category


def delete_category(session: Session, category: NotificationCategory) -> int:
    """Мягко удалить категорию: `is_active=false`, уведомления не трогаем (1.22).

    Жёсткое удаление запрещено сознательно: у уже разосланных уведомлений
    категория — часть истории и отчёта (§11.5). Отключённая категория пропадает
    из выбора, но остаётся у старых уведомлений.
    """
    category.is_active = False
    session.flush()
    logger.info("Категория уведомлений id=%s отключена (мягкое удаление)", category.id)
    return int(category.id)

