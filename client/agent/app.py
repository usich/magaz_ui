"""HTTP-приложение агента (§5.1) — состав этапа Э2.

Реализованы три точки, на которых держится «канал связи»:

* `GET /` — проверка живости **без токена** (перенос v2 1:1, §5.1.1);
* `GET /health` — мониторинг и идентификация: версия, ОС, рабочие пути, `busy`,
  сессии, версии учётных данных 1С (П-1) и списка оборудования (Д-1/Д-2);
* `GET /version` — версия агента и рабочие пути (§5.1.2).

Авторизация: `X-Agent-Token` из конфига магазина плюс, если задан
`allowed_server_ips`, проверка IP источника (§5.1, §8.1). Эндпоинты 1С-операций
(`/check-files`, `/run-merge`, `/run-update-db`, `/download-file`,
`/send-message`) и уведомления подключаются на этапах Э3–Э5.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status

from client.agent.config import AgentConfig, describe_os
from client.agent.models import AgentVersionResponse
from shared.schemas import BusyState, HealthResponse
from shared.timeutil import utcnow
from shared.version import APP_VERSION

logger = logging.getLogger(__name__)

__all__ = ["create_agent_app"]


def create_agent_app(
    config: AgentConfig,
    *,
    started_at: datetime | None = None,
    config_path: Path | None = None,
) -> FastAPI:
    """Собрать приложение агента по его конфигу (§7.2).

    `started_at` передаётся явно в тестах, чтобы `uptime_sec` был предсказуем.
    """
    started = started_at or utcnow()
    app = FastAPI(
        title=f"MagazAgent — магазин {config.store_id or '—'}",
        version=APP_VERSION,
        description="Агент обновления 1С на кассе: мониторинг, операции, уведомления.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    def uptime_sec() -> int:
        """Сколько секунд работает служба (для `/health`)."""
        return max(0, int((utcnow() - started).total_seconds()))

    def source_host(request: Request) -> str:
        """IP источника запроса (для проверки «только сервер», §5.1)."""
        return request.client.host if request.client else "unknown"

    def require_agent_token(request: Request) -> None:
        """Авторизация сервера: токен магазина + вторая линия — IP источника."""
        if not config.agent_token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Токен агента не задан в agent-config.json",
            )

        token = request.headers.get("x-agent-token", "").strip()
        host = source_host(request)
        if not token or token != config.agent_token:
            logger.warning("Отклонён запрос без верного X-Agent-Token (источник %s)", host)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Неверный или отсутствующий X-Agent-Token",
            )

        allowed = [ip.strip() for ip in config.allowed_server_ips if ip and ip.strip()]
        if allowed and host not in allowed:
            logger.warning(
                "Отклонён запрос с чужого IP %s (разрешены: %s)", host, ", ".join(allowed)
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Запрос пришёл не с адреса сервера",
            )

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        """Перенос v2 1:1: `{"status":"online"}` — без токена (§5.1.1)."""
        return {"status": "online"}

    @app.get(
        "/health",
        response_model=HealthResponse,
        dependencies=[Depends(require_agent_token)],
        summary="Состояние агента",
        tags=["agent"],
    )
    def health() -> HealthResponse:
        """Мониторинг и идентификация магазина (§5.1.2).

        `busy` и `sessions` наполняются исполнителем операций на этапе Э3; в Э2
        агент отвечает честно: операций нет, сессий нет.
        """
        return HealthResponse(
            store_id=config.store_id or None,
            agent_version=APP_VERSION,
            uptime_sec=uptime_sec(),
            work_dir=str(config.work_path),
            base_1c_path=config.base_1c_path,
            os=describe_os(),
            busy=BusyState(),
            sessions=[],
            credentials_version=config.credentials_version,
            devices_version=config.devices_version,
        )

    @app.get(
        "/version",
        response_model=AgentVersionResponse,
        dependencies=[Depends(require_agent_token)],
        summary="Версия агента и рабочие пути",
        tags=["agent"],
    )
    def version() -> AgentVersionResponse:
        """Версия агента и рабочие пути (§5.1.2) — для диагностики на кассе."""
        return AgentVersionResponse(
            store_id=config.store_id,
            store_name=config.store_name,
            agent_version=APP_VERSION,
            port=config.port,
            work_dir=str(config.work_path),
            base_1c_path=config.base_1c_path,
            default_file_name=config.default_file_name,
            config_path=str(config_path) if config_path else None,
            uptime_sec=uptime_sec(),
            os=describe_os(),
            credentials_version=config.credentials_version,
            devices_version=config.devices_version,
        )

    app.state.agent_config = config
    app.state.agent_started_at = started
    return app
