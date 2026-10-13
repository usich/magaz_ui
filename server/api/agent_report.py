"""Самодоклады агентов `/api/v1/agent/*` (§5.2.2, Q46).

«Второй контур достижимости»: если касса видит сервер, агент сам сообщает, что
жив, и приносит результат опроса оборудования подсети. Все точки закрыты
зависимостью `get_admin_store` — токен `X-Agent-Token` плюс контроль IP (§8.1).

Реализовано в Э1: `POST /health` (проверка канала, обновление `last_seen_at`,
отчёт о расхождении версий учётных данных П-1 и списка устройств Д-1).
Наполнение остальных точек — этапы Э2 (`/receipts`), Э4 (`/commands/{id}/result`),
Э4Б (`/devices/status`).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.config import API_PREFIX
from server.core.dependencies import get_admin_store, get_db
from server.db.models import Store
from shared.schemas import AgentHealthReport
from shared.timeutil import to_iso_z, utcnow

logger = logging.getLogger(__name__)

__all__ = ["router"]

router = APIRouter(prefix=f"{API_PREFIX}/agent", tags=["agent"])


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
