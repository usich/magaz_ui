"""API оборудования подсети `/api/v1/admin/*` (§5.2.2, §11.7) — состав Э4Б.

Что закрывает модуль (Д-1):

* CRUD списка устройств магазина (`/stores/{id}/devices`) с мягким удалением
  «Отключить из опроса» (`is_active=false`);
* «Проверить сейчас» по магазину и по отдельным строкам: сервер сам просит
  кассу выполнить `POST /devices/check-now` и записывает ответ — подсеть
  магазина сервер не сканирует (§3.3, §7.6);
* сводку по всем магазинам (`/devices/matrix`) для страницы `/devices` и
  плитки «Устройства онлайн/всего» на главной (§11.1, §11.7).

Роутер уже защищён `require_active_admin` — «новый эндпоинт по умолчанию
защищён», как в `server/api/stores.py`.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from math import ceil
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from server.core.client_http import (
    AgentAuthError,
    AgentClient,
    AgentOfflineError,
    AgentProtocolError,
    build_agent_target,
)
from server.core.context import AppContext
from server.core.dependencies import (
    get_client_ip,
    get_context,
    get_db,
    require_active_admin,
)
from server.db.models import AdminUser, StoreDevice
from server.services import audit_service, device_service, settings_service, store_service
from shared.enums import CheckType, DeviceStatus
from shared.schemas import (
    DeviceCheckRequest,
    DeviceCheckResult,
    DeviceCheckSpec,
    DeviceStatusBatch,
)
from shared.timeutil import to_iso_z, utcnow

logger = logging.getLogger(__name__)

__all__ = ["router"]

router = APIRouter(tags=["admin-devices"], dependencies=[Depends(require_active_admin)])

#: Запас к таймауту HTTP-запроса к кассе: одна проверка идёт ≤ `device_ping_timeout_ms`
CHECK_REQUEST_MARGIN_SEC = 3.0
#: Нижняя граница таймаута «Проверить сейчас» (локальная сеть, но касса занята)
MIN_CHECK_TIMEOUT_SEC = 5.0


class DeviceOut(BaseModel):
    """Строка списка оборудования (страница `/devices` и карточка магазина)."""

    id: int
    store_id: int
    store_name: str | None = None
    cluster_name: str | None = None
    store_is_active: bool = True
    store_is_online: bool = False
    name: str
    ip_address: str
    device_type: str | None = None
    check_type: str
    check_port: int | None = None
    is_active: bool
    last_status: str | None = None
    last_seen_at: str | None = None
    last_latency_ms: int | None = None
    last_error: str | None = None
    comment: str | None = None
    is_online: bool = False

    @classmethod
    def from_model(
        cls,
        device: StoreDevice,
        *,
        now: datetime | None = None,
        device_window_sec: int = 180,
        store_window_sec: int = 45,
    ) -> "DeviceOut":
        """Собрать строку: «онлайн» устройства и магазина считаются на лету."""
        store = device.store
        reference = now or utcnow()
        return cls(
            id=device.id,
            store_id=device.store_id,
            store_name=store.name if store is not None else None,
            cluster_name=store.cluster_name if store is not None else None,
            store_is_active=bool(store.is_active) if store is not None else True,
            store_is_online=(
                store.is_online(reference, store_window_sec) if store is not None else False
            ),
            name=device.name,
            ip_address=device.ip_address,
            device_type=device.device_type,
            check_type=str(device.check_type),
            check_port=device.check_port,
            is_active=bool(device.is_active),
            last_status=device.last_status,
            last_seen_at=to_iso_z(device.last_seen_at),
            last_latency_ms=device.last_latency_ms,
            last_error=device.last_error,
            comment=device.comment,
            is_online=device.is_online(reference, device_window_sec),
        )


class DeviceCreateRequest(BaseModel):
    """Тело `POST /stores/{id}/devices` (§11.7: «Добавить устройство»)."""

    name: str
    ip_address: str
    device_type: str | None = None
    check_type: str = CheckType.ICMP.value
    check_port: int | None = None
    comment: str | None = None


class DeviceUpdateRequest(BaseModel):
    """Тело `PUT /stores/{id}/devices/{device_id}`: только изменяемые поля."""

    name: str | None = None
    ip_address: str | None = None
    device_type: str | None = None
    check_type: str | None = None
    check_port: int | None = None
    clear_check_port: bool = False
    is_active: bool | None = None
    comment: str | None = None


class DeviceCheckRunRequest(BaseModel):
    """Тело `POST /stores/{id}/devices/check`: кого проверять (пусто — всех)."""

    device_ids: list[int] = Field(default_factory=list)


class DeviceCheckResponse(BaseModel):
    """Ответ «Проверить сейчас»: что вернула касса и что записано в БД (§11.7)."""

    store_id: int
    checked_at: str
    agent_ok: bool = True
    message: str = ""
    checked: int = 0
    updated: int = 0
    ignored: int = 0
    results: list[DeviceCheckResult] = Field(default_factory=list)


class DevicesMatrixOut(BaseModel):
    """Сводка по всем магазинам: строки и счётчики страницы `/devices` (§11.7)."""

    rows: list[DeviceOut] = Field(default_factory=list)
    total: int = 0
    online: int = 0
    offline: int = 0
    unknown: int = 0
    stores: int = 0
    window_sec: int = 180



# --- Вспомогательные функции ------------------------------------------------


def _get_store_or_404(session: Session, store_id: int):
    """Магазин по id; отсутствие — `404` (§5.2.2)."""
    try:
        return store_service.get_store(session, store_id)
    except store_service.StoreNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _get_device_or_404(session: Session, store_id: int, device_id: int) -> StoreDevice:
    """Устройство магазина; чужой id — `404` (устройства других магазинов не видны)."""
    try:
        return device_service.get_device(session, store_id, device_id)
    except device_service.DeviceNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _online_window(session: Session) -> int:
    """Окно «онлайн» магазина из настроек §4.9 (то же, что в §11.2)."""
    return max(1, settings_service.get_int(session, "online_window_sec"))


# --- CRUD списка устройств (§5.2.2, §11.7) ----------------------------------


@router.get(
    "/stores/{store_id}/devices",
    response_model=list[DeviceOut],
    summary="Оборудование магазина",
)
def list_store_devices(
    store_id: int,
    session: Annotated[Session, Depends(get_db)],
    include_inactive: bool = True,
) -> list[DeviceOut]:
    """Устройства магазина — список в карточке и на странице `/devices` (§11.7)."""
    _get_store_or_404(session, store_id)
    window = _online_window(session)
    devices = device_service.list_devices_for_store(
        session, store_id, include_inactive=include_inactive
    )
    return [
        DeviceOut.from_model(item, device_window_sec=device_service.devices_window_sec(session),
                             store_window_sec=window)
        for item in devices
    ]


@router.post(
    "/stores/{store_id}/devices",
    response_model=DeviceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить устройство",
)
def create_store_device(
    store_id: int,
    payload: DeviceCreateRequest,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> DeviceOut:
    """Завести устройство подсети: имя, IP, тип и способ проверки (§11.7).

    Любое изменение списка поднимает `stores.devices_version` — по расхождению
    версий диспетчер дослает список агенту (§7.6).
    """
    store = _get_store_or_404(session, store_id)
    try:
        device = device_service.create_device(
            session,
            store,
            name=payload.name,
            ip_address=payload.ip_address,
            device_type=payload.device_type,
            check_type=payload.check_type,
            check_port=payload.check_port,
            comment=payload.comment,
        )
    except device_service.DeviceValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    audit_service.record(
        session,
        action="device_create",
        actor=admin.username,
        entity="store_device",
        entity_id=device.id,
        details={
            "store_id": store.id,
            "name": device.name,
            "ip_address": device.ip_address,
            "check_type": str(device.check_type),
            "devices_version": store.devices_version,
        },
        ip_address=get_client_ip(request),
    )
    session.commit()
    return DeviceOut.from_model(
        device,
        device_window_sec=device_service.devices_window_sec(session),
        store_window_sec=_online_window(session),
    )


@router.get(
    "/stores/{store_id}/devices/{device_id}",
    response_model=DeviceOut,
    summary="Карточка устройства",
)
def get_store_device(
    store_id: int,
    device_id: int,
    session: Annotated[Session, Depends(get_db)],
) -> DeviceOut:
    """Одно устройство магазина (§5.2.2)."""
    device = _get_device_or_404(session, store_id, device_id)
    return DeviceOut.from_model(
        device,
        device_window_sec=device_service.devices_window_sec(session),
        store_window_sec=_online_window(session),
    )



@router.put(
    "/stores/{store_id}/devices/{device_id}",
    response_model=DeviceOut,
    summary="Изменить устройство",
)
def update_store_device(
    store_id: int,
    device_id: int,
    payload: DeviceUpdateRequest,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> DeviceOut:
    """Изменить карточку устройства или вернуть его в опрос (`is_active=true`)."""
    store = _get_store_or_404(session, store_id)
    device = _get_device_or_404(session, store_id, device_id)
    try:
        device_service.update_device(
            session,
            store,
            device,
            name=payload.name,
            ip_address=payload.ip_address,
            device_type=payload.device_type,
            check_type=payload.check_type,
            check_port=payload.check_port,
            clear_check_port=payload.clear_check_port,
            is_active=payload.is_active,
            comment=payload.comment,
        )
    except device_service.DeviceValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    audit_service.record(
        session,
        action="device_update",
        actor=admin.username,
        entity="store_device",
        entity_id=device.id,
        details={
            "store_id": store.id,
            "is_active": bool(device.is_active),
            "devices_version": store.devices_version,
        },
        ip_address=get_client_ip(request),
    )
    session.commit()
    return DeviceOut.from_model(
        device,
        device_window_sec=device_service.devices_window_sec(session),
        store_window_sec=_online_window(session),
    )


@router.delete(
    "/stores/{store_id}/devices/{device_id}",
    response_model=DeviceOut,
    summary="Отключить устройство из опроса",
)
def deactivate_store_device(
    store_id: int,
    device_id: int,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> DeviceOut:
    """Мягкое удаление (§11.7): `is_active=false` — устройство уходит из опроса.

    Строку не удаляем: история статусов и задержек нужна при разборе «камера
    пропала», а вернуться в опрос можно той же карточкой (`is_active=true`).
    """
    store = _get_store_or_404(session, store_id)
    device = _get_device_or_404(session, store_id, device_id)
    device_service.deactivate_device(session, store, device)
    audit_service.record(
        session,
        action="device_deactivate",
        actor=admin.username,
        entity="store_device",
        entity_id=device.id,
        details={"store_id": store.id, "devices_version": store.devices_version},
        ip_address=get_client_ip(request),
    )
    session.commit()
    return DeviceOut.from_model(
        device,
        device_window_sec=device_service.devices_window_sec(session),
        store_window_sec=_online_window(session),
    )



# --- «Проверить сейчас» (§11.7, Д-1) ----------------------------------------


def _specs_for(devices: list[StoreDevice]) -> list[DeviceCheckSpec]:
    """Адреса и способы проверки для `POST /devices/check-now` (§5.1.2)."""
    return [
        DeviceCheckSpec(
            ip=item.ip_address,
            check_type=str(item.check_type),
            port=item.check_port,
        )
        for item in devices
    ]


def _check_timeout_sec(specs: list[DeviceCheckSpec], timeout_ms: int) -> float:
    """Таймаут HTTP-запроса к кассе: проверок не больше 16 за раз (§7.6)."""
    batches = ceil(len(specs) / 16) or 1
    return max(
        MIN_CHECK_TIMEOUT_SEC,
        batches * (timeout_ms / 1000 + CHECK_REQUEST_MARGIN_SEC) + CHECK_REQUEST_MARGIN_SEC,
    )


@router.post(
    "/stores/{store_id}/devices/check",
    response_model=DeviceCheckResponse,
    summary="Проверить оборудование сейчас",
)
async def check_store_devices(
    store_id: int,
    request: Request,
    context: Annotated[AppContext, Depends(get_context)],
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    payload: DeviceCheckRunRequest | None = None,
) -> DeviceCheckResponse:
    """«Проверить сейчас» (§11.7): касса опрашивает устройства магазина, сервер пишет итог.

    Адреса берутся только из `store_devices` — агент не сканирует подсеть (§7.6).
    Касса недоступна → `agent_ok=false` и статус `unknown`, а не `offline`: не
    путаем «нет связи с кассой» и «нет связи с камерой» (§4.11). Ответ в этом
    случае всё равно `200`: админ должен видеть причину, а не «ошибку 503».

    Сессия БД открывается внутри: запрос асинхронный (httpx), а работа с SQLite
    короткая — как в проверке магазина `POST /stores/{id}/check` (Д-3).
    """
    wanted = set(payload.device_ids) if payload else set()
    with context.db.session() as session:
        store = _get_store_or_404(session, store_id)
        devices = [
            item
            for item in device_service.list_devices_for_store(
                session, store_id, include_inactive=False
            )
            if not wanted or item.id in wanted
        ]
        checked_at = to_iso_z(utcnow()) or ""
        if not devices:
            return DeviceCheckResponse(
                store_id=store.id,
                checked_at=checked_at,
                agent_ok=False,
                message="В списке магазина нет активных устройств — опрашивать нечего",
            )

        specs = _specs_for(devices)
        timeout_ms = settings_service.get_int(session, device_service.TIMEOUT_SETTING)
        target = build_agent_target(store, context.settings)
        details = {"store_id": store.id, "devices": len(specs)}

        try:
            async with AgentClient(
                target,
                timeout=_check_timeout_sec(specs, timeout_ms),
                transport=context.agent_transport,
            ) as client:
                results = await client.check_devices_now(DeviceCheckRequest(devices=specs))
        except AgentOfflineError as exc:
            summary = device_service.apply_status_batch(
                session,
                store,
                DeviceStatusBatch(
                    store_id=store.id,
                    agent_ok=False,
                    devices=[
                        DeviceCheckResult(
                            ip=item.ip_address,
                            status=DeviceStatus.UNKNOWN,
                            error="касса не ответила",
                        )
                        for item in devices
                    ],
                ),
            )
            audit_service.record(
                session,
                action="device_check",
                actor=admin.username,
                entity="store",
                entity_id=store.id,
                details={**details, "agent_ok": False, "reason": "offline"},
                ip_address=get_client_ip(request),
            )
            session.commit()
            logger.warning("Магазин id=%s: «Проверить сейчас» — касса не ответила (%s)", store.id, exc.message)
            return DeviceCheckResponse(
                store_id=store.id,
                checked_at=checked_at,
                agent_ok=False,
                message=f"Касса не ответила: {exc.message}",
                checked=len(specs),
                updated=summary["updated"],
                ignored=summary["ignored"],
            )
        except AgentAuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"Агент отклонил запрос статусом {exc.status_code}: "
                    "проверьте токен магазина и IP источника"
                ),
            ) from exc
        except AgentProtocolError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.message
            ) from exc

        summary = device_service.apply_status_batch(
            session, store, DeviceStatusBatch(store_id=store.id, devices=results)
        )
        audit_service.record(
            session,
            action="device_check",
            actor=admin.username,
            entity="store",
            entity_id=store.id,
            details={**details, "agent_ok": True, "updated": summary["updated"]},
            ip_address=get_client_ip(request),
        )
        session.commit()

    online = sum(1 for item in results if str(item.status) == DeviceStatus.ONLINE.value)
    return DeviceCheckResponse(
        store_id=store_id,
        checked_at=checked_at,
        agent_ok=True,
        message=f"Проверено устройств: {len(results)}, онлайн: {online}",
        checked=len(results),
        updated=summary["updated"],
        ignored=summary["ignored"],
        results=results,
    )



# --- Сводка по всем магазинам (§11.7) ---------------------------------------


@router.get(
    "/devices/matrix",
    response_model=DevicesMatrixOut,
    summary="Сводная таблица оборудования",
)
def devices_matrix(
    session: Annotated[Session, Depends(get_db)],
    store_id: Annotated[int | None, Query(ge=1)] = None,
    cluster: Annotated[str | None, Query(max_length=200)] = None,
    device_type: Annotated[str | None, Query(max_length=32)] = None,
    status_filter: Annotated[
        str | None, Query(alias="status", pattern="^(all|online|offline|unknown)$")
    ] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    include_inactive: bool = True,
) -> DevicesMatrixOut:
    """Таблица `/devices`: строки «магазин × устройство» и счётчики (§11.7).

    Те же числа использует плитка главной «Устройства онлайн/всего» (§11.1):
    «онлайн» считается по окну `device_online_window_sec`, а не хранится (§4.11).
    """
    device_window = device_service.devices_window_sec(session)
    store_window = _online_window(session)
    try:
        devices = device_service.list_devices(
            session,
            store_id=store_id,
            cluster=cluster,
            device_type=device_type,
            status=status_filter,
            search=search,
            include_inactive=include_inactive,
        )
    except device_service.DeviceValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    rows = [
        DeviceOut.from_model(
            item, device_window_sec=device_window, store_window_sec=store_window
        )
        for item in devices
    ]
    online = sum(1 for row in rows if row.is_online)
    offline = sum(1 for row in rows if row.last_status == DeviceStatus.OFFLINE.value)
    unknown = sum(
        1 for row in rows if row.last_status in (None, DeviceStatus.UNKNOWN.value)
    )
    return DevicesMatrixOut(
        rows=rows,
        total=len(rows),
        online=online,
        offline=offline,
        unknown=unknown,
        stores=len({row.store_id for row in rows}),
        window_sec=device_window,
    )

