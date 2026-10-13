"""Админский API `/api/v1/admin/*` (§5.2.2).

В Э1 (каркас) реализованы узлы, без которых дашборд не поднимается:
вход/выход администратора, «кто я», системная информация, настройки §4.9 и
журнал аудита. CRUD магазинов, задачи, файлы, устройства и уведомления
добавляются на этапах Э2–Э5Б.

Все защищённые точки собраны в `protected_router` — правило «новый эндпоинт
по умолчанию защищён» работает по умолчанию, а не по забывчивости.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from server.api import operations as operations_api
from server.api import stores as stores_api
from server.config import API_PREFIX
from server.core.context import AppContext
from server.core.dependencies import get_client_ip, get_context, get_db, require_active_admin
from server.db.models import AdminUser, AuditLog, Store
from server.services import admin_service, audit_service, command_service, settings_service, store_service
from shared.timeutil import to_iso_z

logger = logging.getLogger(__name__)

__all__ = ["router", "system_info_payload"]

#: 422 без deprecation-предупреждения Starlette (`HTTP_422_UNPROCESSABLE_CONTENT`
#: появился в новых релизах, старое имя помечено устаревшим)
HTTP_422 = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)

# --- Схемы ответов/запросов -------------------------------------------------


class LoginRequest(BaseModel):
    """Тело `POST /auth/login`."""

    username: str
    password: str


class AdminOut(BaseModel):
    """Администратор в API (хеш пароля наружу не уходит никогда)."""

    id: int
    username: str
    is_active: bool
    last_login_at: datetime | None = None

    @classmethod
    def from_model(cls, admin: AdminUser) -> "AdminOut":
        return cls(
            id=admin.id,
            username=admin.username,
            is_active=admin.is_active,
            last_login_at=admin.last_login_at,
        )


class SettingOut(BaseModel):
    """Настройка §4.9 с признаком «значение по умолчанию»."""

    key: str
    value: str
    default: str
    is_default: bool
    description: str


class SettingsUpdateRequest(BaseModel):
    """Тело `PUT /settings`: набор ключ → значение."""

    values: dict[str, str] = Field(default_factory=dict)


class AuditOut(BaseModel):
    """Событие аудита (§4.10)."""

    id: int
    actor: str | None = None
    action: str
    entity: str | None = None
    entity_id: str | None = None
    details: dict[str, Any] | None = None
    ip_address: str | None = None
    created_at: str | None = None

    @classmethod
    def from_model(cls, entry: AuditLog) -> "AuditOut":
        return cls(
            id=entry.id,
            actor=entry.actor,
            action=entry.action,
            entity=entry.entity,
            entity_id=entry.entity_id,
            details=entry.details,
            ip_address=entry.ip_address,
            created_at=to_iso_z(entry.created_at),
        )


#: Пояснения к ключам настроек — показываются в UI `/settings`
SETTINGS_DESCRIPTIONS: dict[str, str] = {
    "health_interval_sec": "Интервал опроса агентов, с",
    "health_timeout_sec": "Таймаут одного запроса к агенту, с",
    "online_window_sec": "Окно, в котором магазин считается онлайн, с",
    "health_concurrency": "Параллелизм опроса агентов",
    "check_poll_interval_sec": "Интервал опроса /check-merge и /check-update-db, с",
    "default_file_name": "Имя файла обновления по умолчанию",
    "command_retry_max": "Попытки отправки команды агенту",
    "log_excerpt_limit_bytes": "Обрезка лога при сохранении в БД, байт",
    # Д-7 (Э4): порог ожидания операции и период сверки учётных данных (П-1)
    "command_poll_timeout_sec": "Предел ожидания операции 1С (после — статус timeout), с",
    "credentials_push_interval_sec": "Как часто сверять версию учётных данных 1С на кассе, с",
    "notification_snooze_min": "Через сколько минут вернуть отложенное уведомление",
    "device_check_interval_sec": "Интервал опроса оборудования подсети агентом, с",
    "device_online_window_sec": "Окно, в котором устройство считается онлайн, с",
    "device_ping_timeout_ms": "Таймаут ping/TCP-connect, мс",
}

# --- Роутеры ----------------------------------------------------------------

router = APIRouter(prefix=f"{API_PREFIX}/admin", tags=["admin"])
auth_router = APIRouter(prefix="/auth", tags=["admin-auth"])
#: Всё, что попадает сюда, автоматически требует действующего администратора
protected_router = APIRouter(dependencies=[Depends(require_active_admin)])
#: Магазины (Э2): CRUD, токен, учётные данные 1С, «проверить сейчас» (§5.2.2, §11.2)
router.include_router(stores_api.router)
#: Операции (Э4): одиночные и массовые действия, история задач, матрица файлов
#: (§5.2.2, §11.3, §11.4, §11.6)
router.include_router(operations_api.router)


# --- Вход/выход (§4.2, §8.1) ------------------------------------------------


@auth_router.post("/login", response_model=AdminOut, summary="Вход в дашборд")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    context: Annotated[AppContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db)],
) -> AdminOut:
    """Проверить логин/пароль и выдать подписанную cookie администратора."""
    admin = admin_service.authenticate(
        session, username=payload.username, password=payload.password
    )
    client_ip = get_client_ip(request)

    if admin is None:
        audit_service.record(
            session,
            action="admin_login_failed",
            actor=(payload.username or "").strip()[:100] or None,
            details={"reason": "invalid_credentials"},
            ip_address=client_ip,
        )
        # Зависимость `get_db` откатит транзакцию при 401, а факт неудачного
        # входа обязан остаться в аудите — коммитим до исключения
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный логин или пароль"
        )

    token = context.session_signer.dump({"admin_id": admin.id, "username": admin.username})
    response.set_cookie(
        key=context.settings.admin_cookie_name,
        value=token,
        max_age=context.settings.session_max_age_sec,
        httponly=True,
        samesite=context.settings.cookie_samesite,
        secure=context.settings.cookie_secure,
        path="/",
    )
    audit_service.record(
        session,
        action="admin_login",
        actor=admin.username,
        entity="admin_user",
        entity_id=admin.id,
        ip_address=client_ip,
    )
    return AdminOut.from_model(admin)


@auth_router.post("/logout", summary="Выход из дашборда")
def logout(
    request: Request,
    response: Response,
    context: Annotated[AppContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db)],
) -> dict[str, bool]:
    """Сбросить cookie. Выход доступен и с уже истёкшей сессией."""
    token = request.cookies.get(context.settings.admin_cookie_name)
    payload = context.session_signer.load(
        token, max_age=context.settings.session_max_age_sec
    )
    response.delete_cookie(key=context.settings.admin_cookie_name, path="/")
    audit_service.record(
        session,
        action="admin_logout",
        actor=(payload or {}).get("username"),
        ip_address=get_client_ip(request),
    )
    return {"ok": True}


@auth_router.get("/me", response_model=AdminOut, summary="Текущий администратор")
def me(admin: Annotated[AdminUser, Depends(require_active_admin)]) -> AdminOut:
    """Кого видит сервер в этой сессии (используется guard-ом SPA)."""
    return AdminOut.from_model(admin)


# --- Системная информация ---------------------------------------------------


def _count(session: Session, model: type) -> int:
    """Количество строк в таблице (для карточек и диагностики)."""
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def system_info_payload(session: Session, context: AppContext) -> dict[str, Any]:
    """Состав диагностики для `/system/info` и плиток главной (§11.1)."""
    dist_dir = context.frontend_dist
    index_file = dist_dir / "index.html"
    online_window = settings_service.get_int(session, "online_window_sec")
    stores_online, stores_offline = store_service.count_by_status(
        session, online_window_sec=online_window
    )
    return {
        "app_name": context.settings.app_name,
        "app_version": context.settings.app_version,
        "database": {
            "kind": "sqlite" if context.settings.is_sqlite else "other",
            "schema_version": _alembic_revision(context),
        },
        "spa": {
            "dist_dir": str(dist_dir),
            "built": index_file.exists(),
        },
        "counts": {
            "stores_total": _count(session, Store),
            "stores_active": int(
                session.scalar(
                    select(func.count()).select_from(Store).where(Store.is_active.is_(True))
                )
                or 0
            ),
            # Э2: «онлайн» считается по окну `online_window_sec`, а не хранится (§3.3)
            "stores_online": stores_online,
            "stores_offline": stores_offline,
            "online_window_sec": online_window,
            "admins_total": _count(session, AdminUser),
            "audit_events": _count(session, AuditLog),
            # Э4: плитка «Задач в работе» на главной (§11.1) — queued/sent/running
            "jobs_active": command_service.count_active(session),
        },
        "settings_keys": len(settings_service.DEFAULT_SETTINGS),
    }


def _alembic_revision(context: AppContext) -> str | None:
    """Текущая ревизия Alembic (без импорта alembic в рантайм API при ошибке)."""
    try:
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext

        with context.db.engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    except Exception:  # pragma: no cover - БД без Alembic (тесты на create_all)
        return None


@protected_router.get("/system/info", summary="Состояние сервера")
def system_info(
    context: Annotated[AppContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """Диагностика: версия, ревизия схемы, собрана ли SPA, счётчики."""
    return system_info_payload(session, context)


# --- Настройки §4.9 ---------------------------------------------------------


@protected_router.get("/settings", response_model=list[SettingOut], summary="Настройки")
def list_settings(session: Annotated[Session, Depends(get_db)]) -> list[SettingOut]:
    """Текущие настройки вместе со значениями по умолчанию из §4.9."""
    current = settings_service.get_all(session)
    return [
        SettingOut(
            key=key,
            value=current.get(key, default),
            default=default,
            is_default=current.get(key, default) == default,
            description=SETTINGS_DESCRIPTIONS.get(key, ""),
        )
        for key, default in settings_service.DEFAULT_SETTINGS.items()
    ]


@protected_router.put("/settings", response_model=list[SettingOut], summary="Изменить настройки")
def update_settings(
    payload: SettingsUpdateRequest,
    request: Request,
    admin: Annotated[AdminUser, Depends(require_active_admin)],
    session: Annotated[Session, Depends(get_db)],
) -> list[SettingOut]:
    """Проверить и сохранить настройки; факт изменения пишется в аудит."""
    if not payload.values:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Нет значений для сохранения")

    for key, value in payload.values.items():
        try:
            settings_service.set_value(session, key, value)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc

    audit_service.record(
        session,
        action="settings_update",
        actor=admin.username,
        entity="settings",
        details={"keys": sorted(payload.values.keys())},
        ip_address=get_client_ip(request),
    )
    return list_settings(session)


# --- Аудит §4.10 ------------------------------------------------------------


@protected_router.get("/audit", response_model=list[AuditOut], summary="Журнал аудита")
def list_audit(
    session: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    actor: Annotated[str | None, Query(max_length=100)] = None,
) -> list[AuditOut]:
    """Последние события аудита (страница `/audit`)."""
    entries = audit_service.list_recent(session, limit=limit, offset=offset, actor=actor)
    return [AuditOut.from_model(entry) for entry in entries]


# --- Сборка роутера ---------------------------------------------------------

router.include_router(auth_router)
router.include_router(protected_router)


