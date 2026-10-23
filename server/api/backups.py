"""API статусов бэкапов `/api/v1/admin/backup-status` и `/backup-list` (Д-21).

Для блока «Бэкапы» на главной: сводка «есть N из M» отдельно по магазинам и
локальным базам; список — чтобы провалиться и увидеть, у кого именно бэкапа нет.
Статусы хранит Backup Monitor в полях `backup_status`/`backup_checked_date`
(история не ведётся — только текущий момент).

Роутер защищён `require_active_admin`, как и остальные админские.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from server.core import backup_monitor
from server.core.context import AppContext
from server.core.dependencies import (
    get_client_ip,
    get_context,
    get_db,
    require_active_admin,
)
from server.db.models import AdminUser, LocalBase, Store
from server.services import audit_service, settings_service
from server.services.backup_service import parse_target_date
from shared.timeutil import to_iso_z

__all__ = ["router"]

router = APIRouter(
    prefix="/backup",
    tags=["admin-backup"],
    dependencies=[Depends(require_active_admin)],
)


# --- Схемы ----------------------------------------------------------------


class BackupCategory(BaseModel):
    """Сводка по одной категории (магазины или локальные базы)."""

    total: int = 0
    checked: int = 0
    ok: int = 0
    missing: int = 0


class BackupStatusOut(BaseModel):
    """Сводка для блока «Бэкапы» на главной (Д-21)."""

    stores: BackupCategory = BackupCategory()
    local_bases: BackupCategory = BackupCategory()
    # Когда Backup Monitor снова сможет опросить (автоматически), ISO-строка
    next_check_iso: str | None = None


class BackupListItem(BaseModel):
    """Строка объединённого списка бэкапов (магазин или локальная база)."""

    kind: str  # "store" | "local_base"
    id: int
    name: str
    backup_status: str  # ok | missing | '' (не проверено)
    backup_error: str | None = None
    backup_checked_at: str | None = None
    backup_checked_date: str | None = None
    backup_check_yesterday: bool = False


# --- Хелперы ---------------------------------------------------------------



def _next_auto_check(session: Session, *, now: datetime | None = None) -> str | None:
    """Когда Backup Monitor снова сможет опросить автоматически (ISO, местное).

    Учитывает окно запросов (Д-21/Д-27): `backup_start_hour` (по умолчанию 10:00)
    и `backup_end_hour` (по умолчанию 15:00). Если сейчас раньше окна — следующий
    запуск сегодня в `start_hour`; внутри окна — следующий интервал
    `backup_interval_sec`; после `end_hour` — завтра в `start_hour`.
    """
    try:
        start_hour = settings_service.get_int(session, "backup_start_hour")
        end_hour = settings_service.get_int(session, "backup_end_hour")
        interval_sec = settings_service.get_int(session, "backup_interval_sec")
    except ValueError:
        return None

    now = datetime.now() if now is None else now
    hour = now.hour
    if hour < start_hour:
        target = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    elif hour >= end_hour:
        # Окно закрыто: следующий автоматический запуск — завтра в start_hour.
        tomorrow = now + timedelta(days=1)
        target = tomorrow.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    else:
        target = now + timedelta(seconds=max(1, interval_sec))
    return target.isoformat(timespec="seconds")

def _today_iso() -> str:
    return date.today().isoformat()


def _category(items: list[tuple[str, str | None, bool]]) -> BackupCategory:
    """Свернуть список `(status, checked_date, yesterday)` в сводку по категории.

    Целевая дата каждого объекта — сегодня или вчера (`backup_check_yesterday`),
    поэтому `ok`/`missing` считаются по совпадению с его целевой датой, а не только
    с сегодняшней (иначе у магазинов с проверкой «за вчера» «0 из 1» при наличном
    бэкапе).
    """
    cat = BackupCategory()
    for status, checked_date, yesterday in items:
        cat.total += 1
        target = parse_target_date(yesterday, reference=date.today())
        if checked_date != target:
            continue
        cat.checked += 1
        if status == "ok":
            cat.ok += 1
        elif status == "missing":
            cat.missing += 1
    return cat


def _list_item(kind: str, obj) -> BackupListItem:
    return BackupListItem(
        kind=kind,
        id=obj.id,
        name=obj.name,
        backup_status=obj.backup_status or "",
        backup_error=obj.backup_error,
        backup_checked_at=to_iso_z(obj.backup_checked_at),
        backup_checked_date=obj.backup_checked_date,
        backup_check_yesterday=obj.backup_check_yesterday,
    )


# --- Эндпоинты -------------------------------------------------------------


@router.get("/status", response_model=BackupStatusOut, summary="Сводка по бэкапам")
def backup_status_endpoint(
    session: Annotated[Session, Depends(get_db)],
) -> BackupStatusOut:
    """Сколько бэкапов есть/нет — для блока «Бэкапы» на главной (Д-21)."""
    store_rows = list(session.scalars(select(Store).where(Store.is_active.is_(True))))
    base_rows = list(session.scalars(select(LocalBase).where(LocalBase.is_active.is_(True))))

    store_items = [
        (s.backup_status or "", s.backup_checked_date, bool(s.backup_check_yesterday))
        for s in store_rows
        if s.backup_check_enabled
    ]
    base_items = [
        (b.backup_status or "", b.backup_checked_date, bool(b.backup_check_yesterday))
        for b in base_rows
        if b.backup_check_enabled
    ]

    next_check_iso = _next_auto_check(session)
    return BackupStatusOut(
        stores=_category(store_items),
        local_bases=_category(base_items),
        next_check_iso=next_check_iso,
    )


def _list_stores(session: Session) -> list[BackupListItem]:
    rows = list(
        session.scalars(
            select(Store)
            .where(Store.is_active.is_(True), Store.backup_check_enabled.is_(True))
            .order_by(Store.name)
        )
    )
    return [_list_item("store", s) for s in rows]


def _list_local_bases(session: Session) -> list[BackupListItem]:
    rows = list(
        session.scalars(
            select(LocalBase)
            .where(LocalBase.is_active.is_(True), LocalBase.backup_check_enabled.is_(True))
            .order_by(LocalBase.name)
        )
    )
    return [_list_item("local_base", b) for b in rows]


@router.get("/stores", response_model=list[BackupListItem], summary="Список бэкапов магазинов")
def backup_stores_endpoint(
    session: Annotated[Session, Depends(get_db)],
) -> list[BackupListItem]:
    """Отдельный список бэкапов по магазинам (Д-21)."""
    return _list_stores(session)


@router.get("/local-bases", response_model=list[BackupListItem], summary="Список бэкапов локальных баз")
def backup_local_bases_endpoint(
    session: Annotated[Session, Depends(get_db)],
) -> list[BackupListItem]:
    """Отдельный список бэкапов по локальным базам (Д-21)."""
    return _list_local_bases(session)


@router.post("/check", summary="Ручная проверка не найденных бэкапов")
def backup_check_endpoint(
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    context: Annotated[AppContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db)],
) -> dict[str, int]:
    """Проверить вручную только те бэкапы, которые ещё не найдены (Д-21).

    Не делает лишних запросов по уже найденным за целевую дату объектам.
    """
    stores = list(session.scalars(select(Store).where(Store.backup_check_enabled.is_(True))))
    bases = list(
        session.scalars(select(LocalBase).where(LocalBase.backup_check_enabled.is_(True)))
    )
    checked = backup_monitor.check_missing(
        session,
        stores,
        bases,
        secret_box=context.secret_box,
    )
    session.commit()

    audit_service.record(
        session,
        action="backup_check_manual",
        actor=admin.username,
        entity="backup",
        details={"checked": checked},
        ip_address=get_client_ip(request),
    )
    return {"checked": checked}
