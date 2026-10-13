"""Самодоклады агентов `/api/v1/agent/*` (§5.2.2, Q46).

«Второй контур достижимости»: если касса видит сервер, агент сам сообщает, что
жив, приносит результат опроса оборудования подсети и досылает итог задачи,
которая выполнилась, пока сервер был недоступен (FR-23). Все точки закрыты
зависимостью `get_admin_store` — токен `X-Agent-Token` плюс контроль IP (§8.1).

Состав: `POST /health` (Э2, отчёт о расхождении версий П-1/Д-1) и
`POST /commands/{id}/result` (Э4). Точка `/receipts` — Э5, `/devices/status` — Э4Б.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.config import API_PREFIX
from server.core.dependencies import get_admin_store, get_db
from server.db.models import Command, Store
from server.services import command_service
from shared.enums import CommandStatus
from shared.schemas import AgentHealthReport, CommandResultReport
from shared.timeutil import to_iso_z, utcnow

logger = logging.getLogger(__name__)

__all__ = ["router"]

router = APIRouter(prefix=f"{API_PREFIX}/agent", tags=["agent"])

#: Какие статусы агент имеет право донести и во что они превращаются (§4.3).
#: `queued`/`sent`/`running` сюда не входят: это состояние сервера, а не результат.
AGENT_RESULT_STATUSES: dict[str, str] = {
    str(CommandStatus.SUCCESS): str(CommandStatus.SUCCESS),
    str(CommandStatus.FAILED): str(CommandStatus.FAILED),
    str(CommandStatus.TIMEOUT): str(CommandStatus.TIMEOUT),
    str(CommandStatus.ERROR): str(CommandStatus.ERROR),
    str(CommandStatus.CANCELED): str(CommandStatus.CANCELED),
}



class AgentHealthAck(BaseModel):
    """Ответ на самодоклад: что сервер намерен дослать агенту (П-1, Д-1)."""

    ok: bool = True
    store_id: int
    server_time: str
    #: Пароль 1С на агенте старее, чем на сервере → нужен push `POST /credentials`
    credentials_push_required: bool = False
    #: Список оборудования на агенте старее → нужен push `POST /devices/config`
    devices_push_required: bool = False


@router.post("/health", response_model=AgentHealthAck, summary="Самодоклад агента")
def agent_health(
    payload: AgentHealthReport,
    store: Annotated[Store, Depends(get_admin_store)],
    session: Annotated[Session, Depends(get_db)],
) -> AgentHealthAck:
    """Принять самодоклад и обновить карточку магазина (§3.3, §5.2.2).

    Поле `store_id` в теле не является доверенным: источник истины — магазин,
    найденный по токену. Расхождение пишем в лог (признак переноса конфига).
    """
    if payload.store_id != store.id:
        logger.warning(
            "Агент магазина id=%s прислал чужой store_id=%s в самодокладе",
            store.id,
            payload.store_id,
        )

    store.last_seen_at = utcnow()
    store.agent_version = payload.agent_version
    store.os_info = payload.os
    session.flush()

    return AgentHealthAck(
        store_id=store.id,
        server_time=to_iso_z(utcnow()) or "",
        credentials_push_required=payload.credentials_version != store.credentials_version,
        devices_push_required=payload.devices_version != store.devices_version,
    )


class CommandResultAck(BaseModel):
    """Ответ на досылку результата задачи: что сервер с ним сделал (FR-23)."""

    ok: bool = True
    command_id: int
    status: str
    duplicate: bool = False
    message: str


@router.post(
    "/commands/{command_id}/result",
    response_model=CommandResultAck,
    summary="Досылка результата задачи",
)
def agent_command_result(
    command_id: int,
    payload: CommandResultReport,
    store: Annotated[Store, Depends(get_admin_store)],
    session: Annotated[Session, Depends(get_db)],
) -> CommandResultAck:
    """Принять итог задачи, о котором агент не смог сообщить сразу (§5.2.2, FR-23).

    Касса могла закончить слияние, пока сервер был недоступен: результат
    досылается этим запросом и «доклеивается» к задаче. Повторная досылка
    безопасна — по завершённой задаче отвечаем `duplicate: true` и ничего не
    переписываем, чтобы поздний (более старый) ответ не затёр настоящий итог.
    """
    command = session.get(Command, command_id)
    if command is None or command.store_id != store.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Задача id={command_id} не найдена у магазина id={store.id}",
        )

    if str(command.status) in command_service.FINISHED_STATUSES:
        return CommandResultAck(
            command_id=command.id,
            status=str(command.status),
            duplicate=True,
            message="Задача уже завершена: результат оставлен прежним",
        )

    target = AGENT_RESULT_STATUSES.get(str(payload.status or "").strip().lower())
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Недопустимый статус результата: ожидается success/failed/timeout/error/canceled"
            ),
        )

    if payload.logs:
        command_service.save_excerpt(session, command, payload.logs)
        command_service.log_from_text(session, command, payload.logs, prefix="лог")
    if payload.message:
        command_service.add_log(session, command, payload.message)
    command_service.set_status(session, command, target)
    if target != str(CommandStatus.SUCCESS):
        command.last_error = (payload.message or command.last_error or "")[:4000] or None
    session.flush()

    logger.info(
        "Магазин id=%s дослал результат задачи id=%s: %s",
        store.id,
        command.id,
        target,
    )
    return CommandResultAck(
        command_id=command.id,
        status=target,
        message="Результат задачи принят",
    )
