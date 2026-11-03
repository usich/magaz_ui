"""API циклов обновления 1С `/api/v1/admin/update-cycles*` (Д-29).

Цикл — граница прохода «скачивание → проверка → слияние → обновление БД». UI
считает статусы этапов только по задачам текущего цикла, поэтому успех прошлой
недели больше не светится зелёным, а старая ошибка не висит красным после
успешного повтора.

Границы задаёт админ: `POST /update-cycles/new` закрывает предыдущий цикл и
открывает новый (`transfer_active` переносит незавершённые задачи, чтобы они не
пропали из статусов). Авто-закрытие по простою — необязательная настройка
`update_cycle_idle_min` (по умолчанию `0` — выключено): проход может длиться
несколько дней.

Роутер защищён `require_active_admin`, как и остальные админские (§5.2.2).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from server.core.dependencies import get_client_ip, get_db, require_active_admin
from server.db.models import AdminUser, UpdateCycle
from server.services import audit_service, settings_service, update_cycle_service
from shared.timeutil import to_iso_z

logger = logging.getLogger(__name__)

__all__ = ["router"]

#: 422 без deprecation-предупреждения Starlette (`HTTP_422_UNPROCESSABLE_CONTENT`
#: появился в новых релизах, старое имя помечено устаревшим) — как в `admin.py`.
HTTP_422 = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)

router = APIRouter(
    prefix="/update-cycles",
    tags=["admin-update-cycles"],
    dependencies=[Depends(require_active_admin)],
)


# --- Схемы ------------------------------------------------------------------


class CycleStatsOut(BaseModel):
    """Счётчики задач цикла (Д-29) — для бейджа «Цикл №15: 34 задачи». """

    total: int = 0
    active: int = 0
    success: int = 0
    failed: int = 0
    finished_stages: int = 0


class UpdateCycleOut(BaseModel):
    """Цикл обновления в API (Д-29)."""

    id: int
    scope: str
    started_at: str | None = None
    started_by: str | None = None
    trigger_kind: str | None = None
    note: str | None = None
    closed_at: str | None = None
    closed_by: str | None = None
    closed_reason: str | None = None
    is_open: bool = True
    stats: CycleStatsOut = Field(default_factory=CycleStatsOut)

    @classmethod
    def from_model(cls, cycle: UpdateCycle, stats: CycleStatsOut) -> "UpdateCycleOut":
        return cls(
            id=cycle.id,
            scope=cycle.scope,
            started_at=to_iso_z(cycle.created_at),
            started_by=cycle.started_by,
            trigger_kind=cycle.trigger_kind,
            note=cycle.note,
            closed_at=to_iso_z(cycle.closed_at),
            closed_by=cycle.closed_by,
            closed_reason=cycle.closed_reason,
            is_open=cycle.is_open,
            stats=stats,
        )


class CurrentCycleOut(BaseModel):
    """Ответ `GET /update-cycles/current`: открытый цикл или `cycle=null`."""

    scope: str
    cycle: UpdateCycleOut | None = None
    #: Д-29: в цикле уже есть завершённые этапы — UI показывает подсказку
    #: «начать новый цикл?» (мягко, без авто-действия)
    suggest_new: bool = False
    #: Текущее значение `update_cycle_idle_min` (`0` — авто-закрытие выключено)
    idle_min: int = 0


class CycleListOut(BaseModel):
    """Ответ `GET /update-cycles`: история циклов, новые — первыми."""

    items: list[UpdateCycleOut]
    total: int


class OpenCycleRequest(BaseModel):
    """Тело `POST /update-cycles/new` (Д-29)."""

    scope: str = Field(default=update_cycle_service.SCOPE_STORES, max_length=16)
    note: str | None = Field(default=None, max_length=500)
    #: Перенести незавершённые задачи прошлого цикла в новый (по умолчанию да)
    transfer_active: bool = True


class OpenCycleOut(BaseModel):
    """Ответ `POST /update-cycles/new`."""

    cycle: UpdateCycleOut
    transferred: int = 0
    message: str


class CloseCycleOut(BaseModel):
    """Ответ `POST /update-cycles/{id}/close`."""

    cycle: UpdateCycleOut
    active_left: int = 0
    message: str


# --- Вспомогательные функции ------------------------------------------------


def _validate_scope(scope: str) -> str:
    """Проверить область цикла (`stores` | `local`); иначе `422`."""
    value = (scope or "").strip()
    if value not in update_cycle_service.SCOPES:
        raise HTTPException(
            status_code=HTTP_422,
            detail=(
                f"Неизвестная область цикла «{value}»: допустимо "
                f"{', '.join(update_cycle_service.SCOPES)}"
            ),
        )
    return value


def _stats_out(session: Session, cycle: UpdateCycle) -> CycleStatsOut:
    """Счётчики цикла в схему API."""
    return CycleStatsOut(**update_cycle_service.stats(session, cycle).as_dict())


def _cycle_out(session: Session, cycle: UpdateCycle) -> UpdateCycleOut:
    """Цикл + счётчики в схему API."""
    return UpdateCycleOut.from_model(cycle, _stats_out(session, cycle))


def _idle_min(session: Session) -> int:
    """Значение `update_cycle_idle_min` (0 — авто-закрытие выключено)."""
    try:
        return settings_service.get_int(session, update_cycle_service.IDLE_SETTING)
    except (ValueError, TypeError):  # настройка ещё не создана
        return 0


# --- Точки API --------------------------------------------------------------


@router.get(
    "/current",
    response_model=CurrentCycleOut,
    summary="Текущий (открытый) цикл обновления",
)
def current_cycle(
    session: Annotated[Session, Depends(get_db)],
    scope: Annotated[str, Query(max_length=16)] = update_cycle_service.SCOPE_STORES,
) -> CurrentCycleOut:
    """Открытый цикл области `scope` — по нему UI считает статусы этапов (Д-29).

    Если цикла ещё нет (свежая установка), возвращается `cycle=null`: первый
    запуск операции создаст цикл автоматически.
    """
    value = _validate_scope(scope)
    cycle = update_cycle_service.current(session, value)
    return CurrentCycleOut(
        scope=value,
        cycle=_cycle_out(session, cycle) if cycle is not None else None,
        suggest_new=update_cycle_service.suggest_new_cycle(session, cycle),
        idle_min=_idle_min(session),
    )


@router.get("", response_model=CycleListOut, summary="История циклов обновления")
def list_cycles(
    session: Annotated[Session, Depends(get_db)],
    scope: Annotated[str | None, Query(max_length=16)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 30,
) -> CycleListOut:
    """История циклов (новые первыми) — для фильтра журнала «показать цикл №15»."""
    stmt = select(UpdateCycle).order_by(UpdateCycle.id.desc()).limit(limit)
    if scope:
        stmt = stmt.where(UpdateCycle.scope == _validate_scope(scope))
    cycles = list(session.scalars(stmt).all())
    return CycleListOut(
        items=[_cycle_out(session, cycle) for cycle in cycles],
        total=len(cycles),
    )


@router.post(
    "/new",
    response_model=OpenCycleOut,
    status_code=status.HTTP_201_CREATED,
    summary="Начать новый цикл обновления",
)
def open_cycle(
    payload: OpenCycleRequest,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> OpenCycleOut:
    """Закрыть открытый цикл и открыть новый (Д-29) — «кнопка» в UI.

    `transfer_active=True` (по умолчанию) переносит незавершённые задачи: админ
    видит «выполняется» и после нажатия, а не пустую строку.
    """
    value = _validate_scope(payload.scope)
    cycle, transferred = update_cycle_service.open_new(
        session,
        scope=value,
        actor=admin.username,
        note=payload.note,
        transfer_active=payload.transfer_active,
    )
    audit_service.record(
        session,
        action="update_cycle_open",
        actor=admin.username,
        entity="update_cycle",
        entity_id=cycle.id,
        details={"scope": value, "transferred": transferred, "note": payload.note},
        ip_address=get_client_ip(request),
    )
    session.commit()
    logger.info(
        "Админ %s начал новый цикл %s id=%s (перенесено задач: %s)",
        admin.username,
        value,
        cycle.id,
        transferred,
    )
    return OpenCycleOut(
        cycle=_cycle_out(session, cycle),
        transferred=transferred,
        message=(
            f"Начат новый цикл №{cycle.id}"
            + (f", перенесено незавершённых задач: {transferred}" if transferred else "")
        ),
    )


@router.post(
    "/{cycle_id}/close",
    response_model=CloseCycleOut,
    summary="Завершить цикл обновления",
)
def close_cycle_endpoint(
    cycle_id: int,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> CloseCycleOut:
    """Закрыть цикл, не открывая новый («проход закончен», Д-29)."""
    cycle = session.get(UpdateCycle, cycle_id)
    if cycle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Цикл id={cycle_id} не найден",
        )
    active_left = update_cycle_service.close_cycle(
        session, cycle, actor=admin.username, reason="manual"
    )
    audit_service.record(
        session,
        action="update_cycle_close",
        actor=admin.username,
        entity="update_cycle",
        entity_id=cycle.id,
        details={"scope": cycle.scope, "active_left": active_left},
        ip_address=get_client_ip(request),
    )
    session.commit()
    return CloseCycleOut(
        cycle=_cycle_out(session, cycle),
        active_left=active_left,
        message=f"Цикл №{cycle.id} завершён",
    )

