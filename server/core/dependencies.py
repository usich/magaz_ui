"""Зависимости FastAPI: контекст приложения, сессия БД, текущий админ, токен агента.

Инвариант I-11: настройки и ресурсы не «зашиты» глобально — приходят сюда
через `app.state.ctx`, поэтому в тестах подменяются одной строкой.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from server.core.context import AppContext
from server.core.security import SecretBox
from server.db.models import AdminUser, Store

logger = logging.getLogger(__name__)

__all__ = [
    "get_admin_store",
    "get_client_ip",
    "get_context",
    "get_current_admin",
    "get_db",
    "get_secret_box",
    "require_active_admin",
]


def get_context(request: Request) -> AppContext:
    """Контекст приложения, собранный в `lifespan`."""
    ctx: AppContext | None = getattr(request.app.state, "ctx", None)
    if ctx is None:  # pragma: no cover - защита от неверной сборки приложения
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Приложение не инициализировано",
        )
    return ctx


def get_db(context: Annotated[AppContext, Depends(get_context)]) -> Iterator[Session]:
    """Сессия БД на один запрос: коммит при успехе, откат при исключении."""
    session = context.db.session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_secret_box(context: Annotated[AppContext, Depends(get_context)]) -> SecretBox:
    """Шифровальщик секретов (пароль 1С, П-1)."""
    return context.secret_box


def get_client_ip(request: Request) -> str:
    """IP клиента для аудита: учитывает `X-Forwarded-For` за реверс-прокси (§8.2)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def get_current_admin(
    request: Request,
    context: Annotated[AppContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db)],
) -> AdminUser:
    """Администратор из подписанной cookie; иначе — `401` (§4.2, §8.1)."""
    token = request.cookies.get(context.settings.admin_cookie_name)
    payload = context.session_signer.load(token, max_age=context.settings.session_max_age_sec)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется вход в дашборд",
        )

    admin_id = payload.get("admin_id")
    admin = session.get(AdminUser, admin_id) if isinstance(admin_id, int) else None
    if admin is None or not admin.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Сессия недействительна")
    return admin


def require_active_admin(
    admin: Annotated[AdminUser, Depends(get_current_admin)],
) -> AdminUser:
    """Тот же админ, но явно «действующий» — точка расширения под роли (Э8)."""
    if not admin.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Учётная запись заблокирована")
    return admin


def get_admin_store(
    request: Request,
    context: Annotated[AppContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db)],
) -> Store:
    """Аутентификация агента: `X-Agent-Token` + контроль IP источника (§8.1, FR-11).

    Две линии защиты: неизвестный/отозванный токен → `401`; токен верный, но
    запрос пришёл не с `stores.ip_address` → `403` (украденный токен бесполезен
    вне туннеля магазина).
    """
    token = request.headers.get("x-agent-token", "").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Не передан X-Agent-Token")

    store = session.query(Store).filter(Store.agent_token == token).one_or_none()
    if store is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неизвестный токен агента")
    if not store.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Магазин отключён")

    source_ip = get_client_ip(request)
    if store.ip_address != source_ip:
        logger.warning(
            "Агент магазина id=%s обратился с чужого IP %s (ожидался %s)",
            store.id,
            source_ip,
            store.ip_address,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Источник запроса не совпадает с IP магазина"
        )
    _ = context  # контекст нужен для единообразия зависимостей (настройки мониторинга)
    return store
