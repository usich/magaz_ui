"""API магазинов `/api/v1/admin/stores/*` (§5.2.2, §11.2) — состав Э2.

Что закрывает модуль:

* CRUD по колонкам ТЗ (§11.2) с мягким удалением (`is_active=false`);
* выдача токена при создании и перевыпуск по кнопке (агент отвечает `401`,
  пока его не перенастроят);
* учётные данные 1С (П-1): логин, пароль «задать/заменить/очистить» — наружу
  уходит только признак `password_set`;
* «Проверить сейчас» (Д-3): живой `GET /health` агента, из которого карточка
  магазина показывает `busy`, сессии, рабочую папку и `base_1c_path`.

Роутер уже защищён `require_active_admin` — «новый эндпоинт по умолчанию
защищён», как и в `server/api/admin.py`.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Annotated, Any

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
from server.core.dependencies import get_client_ip, get_context, get_db, require_active_admin
from server.db.models import AdminUser, Store
from server.services import audit_service, settings_service, store_service
from shared.schemas import AgentSession, BusyState
from shared.timeutil import to_iso_z, utcnow

logger = logging.getLogger(__name__)

__all__ = ["AGENT_CONFIG_FILE_NAME", "build_agent_config_block", "router"]

router = APIRouter(
    prefix="/stores",
    tags=["admin-stores"],
    dependencies=[Depends(require_active_admin)],
)

#: Имя файла конфига агента (§7.2) и каталог установки (§7.1) — подсказка в UI
AGENT_CONFIG_FILE_NAME = "agent-config.json"
AGENT_CONFIG_DIR_HINT = r"C:\MagazAgent"
#: Значение по умолчанию из §7.2 — попадает в подсказку, а не в логику
BASE_1C_PATH_HINT = "C:/Program Files (x86)/1cv8"

ONLINE_WINDOW_SETTING = "online_window_sec"
HEALTH_TIMEOUT_SETTING = "health_timeout_sec"


# --- Схемы ответов/запросов -------------------------------------------------


class StoreOut(BaseModel):
    """Магазин в API (§4.1). Ни пароля, ни токена агента здесь нет."""

    id: int
    name: str
    ip_address: str
    agent_port: int
    cluster_name: str
    database_name: str
    version_1c: str
    user_name_1c: str
    password_set: bool
    credentials_version: int
    devices_version: int
    agent_version: str | None = None
    os_info: str | None = None
    last_seen_at: str | None = None
    is_online: bool = False
    is_active: bool
    comment: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_model(
        cls,
        store: Store,
        *,
        now: datetime | None = None,
        online_window_sec: int = 45,
    ) -> "StoreOut":
        """Собрать ответ; «онлайн» считается по `last_seen_at`, а не хранится."""
        return cls(
            id=store.id,
            name=store.name,
            ip_address=store.ip_address,
            agent_port=store.agent_port,
            cluster_name=store.cluster_name,
            database_name=store.database_name,
            version_1c=store.version_1c,
            user_name_1c=store.user_name_1c,
            password_set=store.password_set,
            credentials_version=store.credentials_version,
            devices_version=store.devices_version,
            agent_version=store.agent_version,
            os_info=store.os_info,
            last_seen_at=to_iso_z(store.last_seen_at),
            is_online=store.is_online(now, online_window_sec),
            is_active=store.is_active,
            comment=store.comment,
            created_at=to_iso_z(store.created_at),
            updated_at=to_iso_z(store.updated_at),
        )


class AgentConfigOut(BaseModel):
    """Блок «что вписать в конфиг агента» (§11.2) — показывается при создании."""

    store_id: int
    server_url: str
    port: int
    agent_token: str
    config_path: str
    config_json: dict[str, Any]
    notes: list[str] = Field(default_factory=list)


class StoreCreatedOut(StoreOut):
    """Ответ на создание магазина: карточка + данные для `agent-config.json`."""

    agent_config: AgentConfigOut


class StoreCreateRequest(BaseModel):
    """Тело `POST /stores` — колонки ТЗ плюс служебные поля (§11.2)."""

    name: str
    ip_address: str
    cluster_name: str
    database_name: str
    version_1c: str
    user_name_1c: str
    agent_port: int = store_service.DEFAULT_AGENT_PORT
    password: str | None = None
    comment: str | None = None


class StoreUpdateRequest(BaseModel):
    """Тело `PUT /stores/{id}`: передаются только изменяемые поля."""

    name: str | None = None
    ip_address: str | None = None
    cluster_name: str | None = None
    database_name: str | None = None
    version_1c: str | None = None
    user_name_1c: str | None = None
    agent_port: int | None = None
    comment: str | None = None
    is_active: bool | None = None


class CredentialsRequest(BaseModel):
    """Тело `PUT /stores/{id}/credentials` (П-1, §8.3).

    Пароль приходит только сюда: значение не логируется, не попадает в аудит и не
    возвращается в ответе — в ответе `StoreOut` виден лишь признак `password_set`.
    """

    user_name_1c: str | None = None
    password: str | None = None
    clear_password: bool = False


class TokenRotateOut(BaseModel):
    """Ответ на перевыпуск токена агента (§11.2)."""

    store_id: int
    agent_token: str
    warning: str


class StoreHealthOut(BaseModel):
    """Живой ответ агента `GET /health` (Д-3) — то, чего нет в таблице `stores`."""

    store_id: int
    status: str = "online"
    checked_at: str
    latency_ms: int | None = None
    agent_version: str
    uptime_sec: int = 0
    os: str | None = None
    work_dir: str | None = None
    base_1c_path: str | None = None
    busy: BusyState = Field(default_factory=BusyState)
    sessions: list[AgentSession] = Field(default_factory=list)
    credentials_version: int = 0
    devices_version: int = 0
    credentials_push_required: bool = False
    devices_push_required: bool = False


# --- Вспомогательные функции ------------------------------------------------


def _online_window(session: Session) -> int:
    """Окно «онлайн» из настроек §4.9 (меняется в UI без перезапуска)."""
    return max(1, settings_service.get_int(session, ONLINE_WINDOW_SETTING))


def _get_or_404(session: Session, store_id: int) -> Store:
    """Магазин по id; отсутствие превращаем в `404` (§5.2.2)."""
    try:
        return store_service.get_store(session, store_id)
    except store_service.StoreNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _server_url(request: Request, context: AppContext) -> str:
    """Адрес сервера для конфига агента: из настройки либо из самого запроса."""
    configured = (context.settings.public_server_url or "").strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def build_agent_config_block(store: Store, *, server_url: str) -> AgentConfigOut:
    """Собрать данные для `agent-config.json` магазина (§7.2, §11.2)."""
    config_json: dict[str, Any] = {
        "store_id": store.id,
        "store_name": store.name,
        "server_url": server_url,
        "agent_token": store.agent_token,
        "port": store.agent_port,
        "base_1c_path": BASE_1C_PATH_HINT,
        "default_file_name": "update",
        "one_c_profile": {
            "user_name": store.user_name_1c,
            "password_enc": None,
            "credentials_version": store.credentials_version,
        },
        "msg_enabled": True,
        "notifications_poll_sec": 20,
        "work_dir": None,
        "devices_version": store.devices_version,
    }
    return AgentConfigOut(
        store_id=store.id,
        server_url=server_url,
        port=store.agent_port,
        agent_token=store.agent_token,
        config_path=f"{AGENT_CONFIG_DIR_HINT}\\{AGENT_CONFIG_FILE_NAME}",
        config_json=config_json,
        notes=[
            "Файл положить рядом с exe агента (путь по умолчанию — §7.2).",
            "Пароль 1С в конфиг не вписывается: его доставляет сервер push-ом "
            "POST /credentials (П-1).",
            "После сохранения конфига магазин станет «онлайн» в течение интервала "
            "мониторинга (§3.3).",
        ],
    )


# --- CRUD магазинов (§5.2.2, §11.2) ----------------------------------------


@router.get("", response_model=list[StoreOut], summary="Список магазинов")
def list_stores_endpoint(
    session: Annotated[Session, Depends(get_db)],
    search: Annotated[str | None, Query(max_length=200)] = None,
    cluster: Annotated[str | None, Query(max_length=200)] = None,
    status_filter: Annotated[
        str | None, Query(alias="status", pattern="^(all|online|offline)$")
    ] = None,
    include_inactive: bool = False,
) -> list[StoreOut]:
    """Магазины с фильтрами страницы `/stores` (§11.2)."""
    window = _online_window(session)
    stores = store_service.list_stores(
        session,
        search=search,
        cluster=cluster,
        status=status_filter,
        include_inactive=include_inactive,
        online_window_sec=window,
    )
    return [StoreOut.from_model(store, online_window_sec=window) for store in stores]


@router.post("", response_model=StoreCreatedOut, summary="Создать магазин")
def create_store_endpoint(
    payload: StoreCreateRequest,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    context: Annotated[AppContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db)],
) -> StoreCreatedOut:
    """Создать магазин и вернуть блок «что вписать в конфиг агента» (§11.2)."""
    try:
        store = store_service.create_store(
            session,
            name=payload.name,
            ip_address=payload.ip_address,
            cluster_name=payload.cluster_name,
            database_name=payload.database_name,
            version_1c=payload.version_1c,
            user_name_1c=payload.user_name_1c,
            agent_port=payload.agent_port,
            password=payload.password,
            secret_box=context.secret_box,
            comment=payload.comment,
        )
    except store_service.StoreValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    audit_service.record(
        session,
        action="store_create",
        actor=admin.username,
        entity="store",
        entity_id=store.id,
        details={"name": store.name, "ip_address": store.ip_address},
        ip_address=get_client_ip(request),
    )

    window = _online_window(session)
    base = StoreOut.from_model(store, online_window_sec=window)
    config_block = build_agent_config_block(
        store, server_url=_server_url(request, context)
    )
    return StoreCreatedOut(**base.model_dump(), agent_config=config_block)


@router.get("/{store_id}", response_model=StoreOut, summary="Карточка магазина")
def get_store_endpoint(
    store_id: int,
    session: Annotated[Session, Depends(get_db)],
) -> StoreOut:
    """Реквизиты и статус магазина (§11.2)."""
    store = _get_or_404(session, store_id)
    return StoreOut.from_model(store, online_window_sec=_online_window(session))


@router.put("/{store_id}", response_model=StoreOut, summary="Изменить магазин")
def update_store_endpoint(
    store_id: int,
    payload: StoreUpdateRequest,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> StoreOut:
    """Изменить реквизиты магазина; смена логина 1С поднимает `credentials_version`."""
    store = _get_or_404(session, store_id)
    changed = payload.model_dump(exclude_unset=True, exclude_none=True)

    try:
        store_service.update_store(session, store, **changed)
    except store_service.StoreValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    audit_service.record(
        session,
        action="store_update",
        actor=admin.username,
        entity="store",
        entity_id=store.id,
        details={"fields": sorted(changed.keys())},
        ip_address=get_client_ip(request),
    )
    return StoreOut.from_model(store, online_window_sec=_online_window(session))


@router.delete("/{store_id}", response_model=StoreOut, summary="Отключить магазин")
def deactivate_store_endpoint(
    store_id: int,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> StoreOut:
    """Мягкое удаление (§5.2.2): магазин исключается из мониторинга и операций."""
    store = _get_or_404(session, store_id)
    store_service.deactivate_store(session, store)
    audit_service.record(
        session,
        action="store_deactivate",
        actor=admin.username,
        entity="store",
        entity_id=store.id,
        details={"name": store.name},
        ip_address=get_client_ip(request),
    )
    return StoreOut.from_model(store, online_window_sec=_online_window(session))


@router.post(
    "/{store_id}/token/rotate", response_model=TokenRotateOut, summary="Перевыпустить токен"
)
def rotate_token_endpoint(
    store_id: int,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> TokenRotateOut:
    """Перевыпуск `agent_token` (§11.2): старый токен перестаёт работать."""
    store = _get_or_404(session, store_id)
    token = store_service.rotate_agent_token(session, store)
    audit_service.record(
        session,
        action="store_token_rotate",
        actor=admin.username,
        entity="store",
        entity_id=store.id,
        details={"name": store.name},
        ip_address=get_client_ip(request),
    )
    return TokenRotateOut(
        store_id=store.id,
        agent_token=token,
        warning=(
            "Агент ответит 401, пока в agent-config.json не будет вписан новый токен: "
            "магазин потребуется перенастроить (§11.2)."
        ),
    )


@router.put(
    "/{store_id}/credentials", response_model=StoreOut, summary="Учётные данные 1С (П-1)"
)
def set_credentials_endpoint(
    store_id: int,
    payload: CredentialsRequest,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    context: Annotated[AppContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db)],
) -> StoreOut:
    """Задать/заменить/очистить логин и пароль 1С (§8.3).

    В аудит уходит только факт изменения: ни значения пароля, ни его шифротекста
    (§8.3, I-7). Наружу в ответе — признак `password_set`.
    """
    store = _get_or_404(session, store_id)
    try:
        store_service.set_credentials(
            session,
            store,
            user_name_1c=payload.user_name_1c,
            password=payload.password,
            clear_password=payload.clear_password,
            secret_box=context.secret_box,
        )
    except store_service.StoreValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    audit_service.record(
        session,
        action="store_credentials_update",
        actor=admin.username,
        entity="store",
        entity_id=store.id,
        details={
            "credentials_version": store.credentials_version,
            "password_set": store.password_set,
            "cleared": bool(payload.clear_password),
        },
        ip_address=get_client_ip(request),
    )
    return StoreOut.from_model(store, online_window_sec=_online_window(session))


@router.post(
    "/{store_id}/check", response_model=StoreHealthOut, summary="Проверить магазин сейчас"
)
async def check_store_endpoint(
    store_id: int,
    context: Annotated[AppContext, Depends(get_context)],
) -> StoreHealthOut:
    """Живой `GET /health` агента (Д-3): `busy`, сессии, пути, версии (П-1, Д-1).

    Сессия БД открывается здесь же, в потоке цикла событий: запрос асинхронный
    (httpx), а работа с SQLite короткая.
    """
    with context.db.session() as session:
        store = _get_or_404(session, store_id)
        target = build_agent_target(store, context.settings)
        timeout = float(settings_service.get_int(session, HEALTH_TIMEOUT_SETTING))
        window = _online_window(session)

        started = time.perf_counter()
        try:
            async with AgentClient(
                target, timeout=timeout, transport=context.agent_transport
            ) as client:
                health = await client.get_health()
        except AgentOfflineError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Магазин недоступен (офлайн): {exc.message}",
            ) from exc
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

        latency_ms = int((time.perf_counter() - started) * 1000)
        checked_at = utcnow()
        store.last_seen_at = checked_at
        store.agent_version = health.agent_version
        store.os_info = health.os
        session.flush()
        credentials_push_required = health.credentials_version != store.credentials_version
        devices_push_required = health.devices_version != store.devices_version
        agent_version = health.agent_version
        session.commit()

    logger.info(
        "Магазин id=%s отвечает: версия %s, задержка %s мс (окно «онлайн» %s с)",
        store_id,
        agent_version,
        latency_ms,
        window,
    )
    return StoreHealthOut(
        store_id=store_id,
        status=health.status,
        checked_at=to_iso_z(checked_at) or "",
        latency_ms=latency_ms,
        agent_version=health.agent_version,
        uptime_sec=health.uptime_sec,
        os=health.os,
        work_dir=health.work_dir,
        base_1c_path=health.base_1c_path,
        busy=health.busy,
        sessions=health.sessions,
        credentials_version=health.credentials_version,
        devices_version=health.devices_version,
        credentials_push_required=credentials_push_required,
        devices_push_required=devices_push_required,
    )
