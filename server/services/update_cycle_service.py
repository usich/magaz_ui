"""Циклы обновления 1С (Д-29) — граница прохода «скачивание → проверка → слияние →
обновление БД».

Зачем сервис существует
-----------------------
Вкладка «Обновление 1С» показывала *последнюю задачу данного вида за всю
историю*: успешное слияние прошлой недели оставалось зелёным и после старта
нового прохода, а старая ошибка светилась красным даже после успешного повтора.
Цикл даёт границу, по которой UI считает статусы этапов, а история задач
(`/jobs`) остаётся неизменной.

Правила (согласованы с заказчиком, Д-29)
----------------------------------------
* Границы задаёт **человек** — кнопка «Начать новый цикл»: проход может длиться
  несколько дней (слияние в пятницу, обновление в понедельник).
* Авто-закрытие по простою выключено по умолчанию (`update_cycle_idle_min=0`);
  включённая настройка — только страховка «админ забыл нажать».
* Если открытого цикла нет, он создаётся при постановке первой задачи: иначе
  задача осталась бы без `cycle_id` и выпала бы из статусов.
* Циклы раздельные для магазинов (`stores`) и локальных баз (`local`).
* При смене цикла **активные** задачи по умолчанию переносятся в новый — админ
  не теряет из вида то, что ещё выполняется (`transfer_active=True`).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from server.db.models import Command, UpdateCycle
from server.services import settings_service
from shared.enums import CommandKind, CommandStatus
from shared.timeutil import utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "CYCLE_KINDS",
    "IDLE_SETTING",
    "LOCAL_KINDS",
    "SCOPES",
    "SCOPE_LOCAL",
    "SCOPE_STORES",
    "STORES_KINDS",
    "CycleStats",
    "close_cycle",
    "current",
    "get_or_open",
    "new_batch_id",
    "open_new",
    "scope_for_kind",
    "stats",
    "suggest_new_cycle",
]

#: Цикл магазинов: кассы с агентами (§4.1)
SCOPE_STORES = "stores"
#: Цикл локальных баз: `1cv8.exe` на машине дашборда (Д-24)
SCOPE_LOCAL = "local"
SCOPES: tuple[str, ...] = (SCOPE_STORES, SCOPE_LOCAL)

#: Д-29: минуты простоя, после которых открытый цикл закрывается сам.
#: `0` — не закрывать никогда (значение по умолчанию): границы задаёт админ.
IDLE_SETTING = "update_cycle_idle_min"

#: Виды задач, которые относятся к циклу магазинов
STORES_KINDS: frozenset[str] = frozenset(
    {
        str(CommandKind.CHECK_FILES),
        str(CommandKind.DOWNLOAD_FILE),
        str(CommandKind.RUN_MERGE),
        str(CommandKind.RUN_UPDATE_DB),
    }
)
#: Виды задач, которые относятся к циклу локальных баз (Д-26)
LOCAL_KINDS: frozenset[str] = frozenset(
    {
        str(CommandKind.CHECK_FILES_LOCAL),
        str(CommandKind.RUN_MERGE_LOCAL),
        str(CommandKind.RUN_UPDATE_DB_LOCAL),
    }
)
#: Все виды задач, попадающие в цикл
CYCLE_KINDS: frozenset[str] = STORES_KINDS | LOCAL_KINDS

#: «Этапы ИБ» цикла: по ним решается, пора ли предложить новый цикл
_STAGE_KINDS: frozenset[str] = frozenset(
    {
        str(CommandKind.RUN_MERGE),
        str(CommandKind.RUN_UPDATE_DB),
        str(CommandKind.RUN_MERGE_LOCAL),
        str(CommandKind.RUN_UPDATE_DB_LOCAL),
    }
)

#: Незавершённые статусы — «в работе» (§4.3)
_ACTIVE_STATUSES: tuple[str, ...] = (
    str(CommandStatus.QUEUED),
    str(CommandStatus.SENT),
    str(CommandStatus.RUNNING),
)


@dataclass(slots=True)
class CycleStats:
    """Счётчики задач цикла для бейджа в UI и для подсказки «начать новый»."""

    total: int = 0
    active: int = 0
    success: int = 0
    failed: int = 0
    finished_stages: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "active": self.active,
            "success": self.success,
            "failed": self.failed,
            "finished_stages": self.finished_stages,
        }


def scope_for_kind(kind: str | CommandKind) -> str | None:
    """Цикл какого вида порождает задача `kind` (`None` — задача вне цикла)."""
    value = str(kind)
    if value in STORES_KINDS:
        return SCOPE_STORES
    if value in LOCAL_KINDS:
        return SCOPE_LOCAL
    return None


def new_batch_id() -> str:
    """Идентификатор «пачки запуска» (Д-29): один запуск — один `batch_id`."""
    return uuid.uuid4().hex


def current(session: Session, scope: str) -> UpdateCycle | None:
    """Открытый цикл области `scope` (последний, если их несколько)."""
    stmt = (
        select(UpdateCycle)
        .where(UpdateCycle.scope == scope, UpdateCycle.closed_at.is_(None))
        .order_by(UpdateCycle.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _last_activity_at(session: Session, cycle: UpdateCycle) -> datetime | None:
    """Время последней активности цикла: его открытие или любая задача в нём."""
    stmt = select(func.max(Command.created_at)).where(Command.cycle_id == cycle.id)
    latest_command = session.scalars(stmt).first()
    if latest_command is None:
        return cycle.created_at
    return max(cycle.created_at, latest_command)


def _active_count(session: Session, cycle: UpdateCycle) -> int:
    """Сколько задач цикла ещё не завершено."""
    active = session.scalar(
        select(func.count(Command.id)).where(
            Command.cycle_id == cycle.id,
            Command.status.in_(list(_ACTIVE_STATUSES)),
        )
    )
    return int(active or 0)


def _is_idle(session: Session, cycle: UpdateCycle, idle_min: int) -> bool:
    """Пора ли закрыть цикл по простою (только если задач в работе нет)."""
    if idle_min <= 0:
        return False
    if _active_count(session, cycle):
        return False
    last_at = _last_activity_at(session, cycle)
    if last_at is None:
        return False
    return (utcnow() - last_at).total_seconds() >= idle_min * 60


def close_cycle(
    session: Session,
    cycle: UpdateCycle,
    *,
    actor: str | None = None,
    reason: str = "manual",
) -> int:
    """Закрыть цикл; вернуть число оставшихся в нём незавершённых задач.

    Задачи не переносятся и не удаляются: они остаются историей закрытого цикла
    (перенос делает `open_new` — для случая «начали новый, а старое ещё бежит»).
    """
    if cycle.is_open:
        cycle.closed_at = utcnow()
        cycle.closed_by = actor
        cycle.closed_reason = reason
        session.flush()
        logger.info("Цикл id=%s (%s) закрыт: %s", cycle.id, cycle.scope, reason)
    return _active_count(session, cycle)



def open_new(
    session: Session,
    *,
    scope: str,
    actor: str | None = None,
    kind: str | CommandKind | None = None,
    note: str | None = None,
    transfer_active: bool = True,
) -> tuple[UpdateCycle, int]:
    """Открыть новый цикл, закрыв предыдущий; вернуть `(цикл, перенесено задач)`.

    `transfer_active=True` переносит незавершённые задачи прошлого цикла в новый:
    админ, нажавший «Начать новый цикл», всё равно должен видеть, что часть касс
    ещё выполняется, а не «—» в строке.
    """
    if scope not in SCOPES:
        raise ValueError(f"Неизвестная область цикла: {scope}")

    kinds = list(STORES_KINDS if scope == SCOPE_STORES else LOCAL_KINDS)
    previous = current(session, scope)
    transferred_ids: list[int] = []
    if previous is not None:
        close_cycle(session, previous, actor=actor, reason="manual")
        if transfer_active:
            transferred_ids = list(
                session.scalars(
                    select(Command.id).where(
                        Command.cycle_id == previous.id,
                        Command.status.in_(list(_ACTIVE_STATUSES)),
                        Command.kind.in_(kinds),
                    )
                ).all()
            )

    cycle = UpdateCycle(
        scope=scope,
        started_by=actor,
        trigger_kind=str(kind) if kind is not None else None,
        note=note,
    )
    session.add(cycle)
    session.flush()

    if transferred_ids:
        session.execute(
            update(Command).where(Command.id.in_(transferred_ids)).values(cycle_id=cycle.id)
        )
        session.flush()

    logger.info(
        "Цикл id=%s (%s) открыт автором %s; перенесено активных задач: %s",
        cycle.id,
        scope,
        actor or "—",
        len(transferred_ids),
    )
    return cycle, len(transferred_ids)



def get_or_open(
    session: Session,
    scope: str,
    *,
    kind: str | CommandKind | None = None,
    actor: str | None = None,
) -> UpdateCycle:
    """Вернуть открытый цикл области или открыть новый (Д-29).

    Вызывается при постановке задачи: без этого первая же задача осталась бы без
    `cycle_id` и выпала бы из статусов. Если включена настройка
    `update_cycle_idle_min`, «зависший» цикл (нет задач в работе, простой больше
    нормы) закрывается и открывается новый.
    """
    try:
        idle_min = settings_service.get_int(session, IDLE_SETTING)
    except (ValueError, TypeError):  # настройка ещё не создана — не закрываем
        idle_min = 0

    cycle = current(session, scope)
    if cycle is not None and _is_idle(session, cycle, idle_min):
        close_cycle(session, cycle, actor=None, reason="idle")
        cycle = None

    if cycle is None:
        cycle, _ = open_new(session, scope=scope, actor=actor, kind=kind)
    return cycle


def stats(session: Session, cycle: UpdateCycle) -> CycleStats:
    """Счётчики задач цикла (для бейджа и подсказки «начать новый»)."""
    stmt = (
        select(Command.status, Command.kind, func.count(Command.id))
        .where(Command.cycle_id == cycle.id)
        .group_by(Command.status, Command.kind)
    )
    result = CycleStats()
    for status, kind, count in session.execute(stmt).all():
        count = int(count)
        result.total += count
        if status in _ACTIVE_STATUSES:
            result.active += count
        elif status == str(CommandStatus.SUCCESS):
            result.success += count
            if kind in _STAGE_KINDS:
                result.finished_stages += count
        else:
            result.failed += count
    return result


def suggest_new_cycle(session: Session, cycle: UpdateCycle | None) -> bool:
    """Стоит ли предложить новый цикл: в текущем уже есть завершённые этапы.

    UI использует это как мягкую подсказку («в цикле уже было слияние — начать
    новый?»), а не как авто-действие: цикл может тянуться несколько дней.
    """
    if cycle is None:
        return False
    return stats(session, cycle).finished_stages > 0

