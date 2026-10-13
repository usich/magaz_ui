"""HTTP-приложение агента (§5.1) — состав этапов Э2–Э3.

Реализованы все эндпоинты §5.1.1 в переносе v2 1:1 (поведение и коды ответов
менять нельзя):

* `GET /` — проверка живости **без токена**;
* `POST /check-files` — наличие `.cf/.cfe/.xml` + размеры и даты (Q52);
* `POST /run-merge` / `GET /check-merge` — слияние и его статус;
* `POST /run-update-db` / `GET /check-update-db` — обновление БД и его статус;
* `POST /download-file` — скачивание пакета с Яндекс.Диска (ФН2, FR-1);
* `POST /send-message` — `msg.exe` + завершение `1cv8.exe`, с признаком
  `killed_1cv8` (§5.1.1, п. 4 расширений).

Плюс точки мониторинга Э2 (`GET /health`, `GET /version`), в которых `busy`
теперь заполняется исполнителем операций, а `interrupted_operation` доносит до
сервера операцию, прерванную перезагрузкой кассы (§7.3).

Авторизация: `X-Agent-Token` из конфига магазина плюс, если задан
`allowed_server_ips`, проверка IP источника (§5.1, §8.1). Повторный запуск
1С-операции, пока идёт предыдущая, отвечает `409 Conflict` (I-13, Q23); лог
обнуляется до постановки задачи в очередь (I-3). Уведомления (`/notify`,
`/notifications/*`) и `/credentials`, `/devices/*` подключаются на этапах Э4–Э5.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from client.agent import credentials, downloader, messenger
from client.agent.config import AgentConfig, describe_os
from client.agent.executor import OperationBusyError, OperationRunner
from client.agent.models import AgentVersionResponse
from client.agent.one_c import (
    LOG_STATE_FAILED,
    LOG_STATE_SUCCESS,
    OneCPaths,
    SUCCESS_MERGE_MSG,
    SUCCESS_UPDATE_MSG,
    check_log_file,
)
from client.agent.processes import DEFAULT_PROVIDER, ProcessProvider
from shared.schemas import (
    CheckFilesParams,
    CheckFilesResponse,
    DownloadParams,
    HealthResponse,
    JobStateResponse,
    MergeParams,
    MessageParams,
    UpdateParams,
)
from shared.timeutil import to_iso_z, utcnow
from shared.version import APP_VERSION

logger = logging.getLogger(__name__)

__all__ = ["create_agent_app"]



# --- Вспомогательные функции ответов (перенос v2 1:1) -----------------------


def _log_state_response(
    state: str,
    logs: str,
    *,
    success_message: str,
    failed_message: str,
    in_progress_message: str,
) -> JSONResponse:
    """Единый формат `/check-*` (перенос v2): `200` успех, `400` ошибка, `202` в работе."""
    if state == LOG_STATE_SUCCESS:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": state, "message": success_message, "logs": logs},
        )
    if state == LOG_STATE_FAILED:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": state, "message": failed_message, "logs": logs},
        )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"status": state, "message": in_progress_message, "logs": logs},
    )


def _mtime_iso(timestamp: float) -> str:
    """Дата файла в формате API: UTC с суффиксом `Z` (§6, `shared.timeutil`)."""
    return to_iso_z(datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None)) or ""


def _file_info(path: Path) -> tuple[bool, int | None, str | None]:
    """Наличие, размер и дата файла обновления (Q52) — без исключений наружу."""
    try:
        stat = path.stat()
    except OSError:
        return False, None, None
    return True, stat.st_size, _mtime_iso(stat.st_mtime)


def create_agent_app(
    config: AgentConfig,
    *,
    started_at: datetime | None = None,
    config_path: Path | None = None,
    paths: OneCPaths | None = None,
    provider: ProcessProvider | None = None,
    runner: OperationRunner | None = None,
    state_path: Path | None = None,
) -> FastAPI:
    """Собрать приложение агента по его конфигу (§7.2).

    `started_at` передаётся явно в тестах, чтобы `uptime_sec` был предсказуем;
    `paths`, `provider`, `runner` — точки подмены для проверок (§7.5, §13):
    реальная 1С, `msg.exe` и `taskkill` в тестах не запускаются.
    """
    started = started_at or utcnow()
    one_c_paths = paths or OneCPaths(
        work_dir=config.work_path, base_1c_path=Path(config.base_1c_path)
    )
    process_provider = provider or DEFAULT_PROVIDER
    operations = runner or OperationRunner(
        paths=one_c_paths,
        state_path=state_path or config.state_path,
        provider=process_provider,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Старт: помечаем прерванную операцию; остановка: гасим воркер (§7.3)."""
        interrupted = operations.mark_interrupted()
        logger.info(
            "MagazAgent запущен: магазин %s, рабочая папка %s, прерванная операция: %s",
            config.store_id or "—",
            one_c_paths.work_dir,
            interrupted.operation if interrupted else "нет",
        )
        try:
            yield
        finally:
            await operations.shutdown()

    app = FastAPI(
        title=f"MagazAgent — магазин {config.store_id or '—'}",
        version=APP_VERSION,
        description="Агент обновления 1С на кассе: мониторинг, операции, уведомления.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    #: Фоновые задачи без 1С (скачивание пакета): держим ссылки, пока они живы
    background: set[asyncio.Task] = set()

    def spawn(coro, *, name: str) -> None:  # noqa: ANN001 - корутина фоновой задачи
        """Запустить фоновую задачу, не блокируя HTTP-слой (§5.1.1).

        Ссылка на задачу живёт до её завершения: иначе сборщик мусора может убить
        скачивание на полпути, а исключение осталось бы никем не замеченным.
        """
        task = asyncio.get_running_loop().create_task(coro, name=name)
        background.add(task)

        def _done(finished: asyncio.Task) -> None:
            background.discard(finished)
            if finished.cancelled():
                return
            error = finished.exception()
            if error is not None:
                logger.error(
                    "Фоновая задача %s завершилась ошибкой: %s", finished.get_name(), error
                )

        task.add_done_callback(_done)

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

    # --- §5.1.1: перенос v2 (поведение и коды ответов 1:1) -------------------
    # Операции §5.1.1 открыты только серверу: токен магазина + контроль IP (§5.1).
    @app.post(
        "/check-files",
        response_model=CheckFilesResponse,
        dependencies=[Depends(require_agent_token)],
        summary="Наличие файлов обновления в рабочей папке",
        tags=["1c"],
    )
    async def check_files(params: CheckFilesParams = Body(...)) -> CheckFilesResponse:
        """`.cf` / `.cfe` / `.xml` в рабочей папке агента (FR-2, расширение Q52).

        Прежние флаги `*_exists` сохранены как есть, добавлены размер и дата
        файла (`*_size`, `*_mtime`) — сервер показывает их в матрице файлов.
        """
        cf_exists, cf_size, cf_mtime = _file_info(one_c_paths.update_file(params.filename, ".cf"))
        cfe_exists, cfe_size, cfe_mtime = _file_info(
            one_c_paths.update_file(params.filename, ".cfe")
        )
        xml_exists, xml_size, xml_mtime = _file_info(
            one_c_paths.update_file(params.filename, ".xml")
        )
        return CheckFilesResponse(
            cf_exists=cf_exists,
            cfe_exists=cfe_exists,
            xml_exists=xml_exists,
            cf_size=cf_size,
            cfe_size=cfe_size,
            xml_size=xml_size,
            cf_mtime=cf_mtime,
            cfe_mtime=cfe_mtime,
            xml_mtime=xml_mtime,
        )

    @app.post(
        "/run-merge",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_agent_token)],
        summary="Слияние конфигурации или расширения",
        tags=["1c"],
    )
    async def run_merge(params: MergeParams = Body(...)) -> dict[str, str]:
        """Запустить слияние в фоне (FR-3, FR-4, FR-9).

        Для расширения передайте `extension_name`, для основной конфигурации —
        не передавайте. Пока идёт операция, повторный запуск отвечает `409`.
        """
        prepared = credentials.apply_profile_password(params, config.one_c_profile)
        try:
            state = await operations.submit_merge(prepared)
        except OperationBusyError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return {
            "message": "Merge process started in the background.",
            "log_path": state.log_file,
        }

    @app.get(
        "/check-merge",
        response_model=JobStateResponse,
        dependencies=[Depends(require_agent_token)],
        summary="Статус последнего слияния",
        tags=["1c"],
    )
    async def check_merge(extension: bool = False) -> JSONResponse:
        """Статус слияния по лог-файлу: `200` успех, `400` ошибка, `202` в работе."""
        log_path = one_c_paths.merge_log("extension" if extension else None)
        state, logs = await check_log_file(log_path, SUCCESS_MERGE_MSG)
        return _log_state_response(
            state,
            logs,
            success_message="Merge completed successfully.",
            failed_message="Merge failed.",
            in_progress_message="Merge is in progress.",
        )

    @app.post(
        "/run-update-db",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_agent_token)],
        summary="Обновление конфигурации базы данных",
        tags=["1c"],
    )
    async def run_update_db(params: UpdateParams = Body(...)) -> dict[str, str]:
        """Запустить обновление БД в фоне (FR-5, FR-6, FR-9).

        `is_fast_update = false` — полный цикл: завершение сеансов → `taskkill
        1cv8.exe` → `/UpdateDBCfg` → разрешение работы пользователей.
        """
        prepared = credentials.apply_profile_password(params, config.one_c_profile)
        try:
            state = await operations.submit_update_db(prepared)
        except OperationBusyError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return {
            "message": "Update DB process started in the background.",
            "log_path": state.log_file,
        }

    @app.get(
        "/check-update-db",
        response_model=JobStateResponse,
        dependencies=[Depends(require_agent_token)],
        summary="Статус последнего обновления БД",
        tags=["1c"],
    )
    async def check_update_db(extension: bool = False) -> JSONResponse:
        """Статус обновления БД: `200` успех, `400` ошибка, `202` в работе."""
        log_path = one_c_paths.update_log("extension" if extension else None)
        state, logs = await check_log_file(log_path, SUCCESS_UPDATE_MSG)
        return _log_state_response(
            state,
            logs,
            success_message="DB update completed successfully.",
            failed_message="DB update failed.",
            in_progress_message="DB update is in progress.",
        )

    @app.post(
        "/download-file",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_agent_token)],
        summary="Скачать пакет обновления с Яндекс.Диска",
        tags=["files"],
    )
    async def download_file(params: DownloadParams = Body(...)) -> dict[str, str]:
        """Скачать пакет в рабочую папку агента в фоне (FR-1, ФН2).

        Имя файла берётся как basename (I-2); тело запроса принимает и `url-file`
        (как в документации), и `url_file`.
        """
        spawn(
            downloader.download_file_from_yandex_disk(
                params.url_file, work_dir=one_c_paths.work_dir
            ),
            name="agent-download",
        )
        return {"message": "Download process started in the background."}

    @app.post(
        "/send-message",
        dependencies=[Depends(require_agent_token)],
        summary="Сообщение пользователям и завершение 1С",
        tags=["1c"],
    )
    async def send_message(params: MessageParams = Body(...)) -> dict[str, object]:
        """Показать сообщение в сессиях и завершить `1cv8.exe` (FR-8).

        Расширение §5.1.1 (п. 4): в ответе есть `killed_1cv8` — сервер знает,
        что `taskkill` реально отработал.
        """
        try:
            killed = await messenger.send_windows_message(
                params.text_message, provider=process_provider
            )
        except Exception as exc:  # служба не должна падать из-за утилит Windows
            logger.exception("Ошибка при отправке сообщения")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
            ) from exc
        return {
            "message": "Message sent and 1cv8 processes terminated.",
            "killed_1cv8": killed,
        }

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

        `busy` честно показывает идущую 1С-операцию (Q23), а
        `interrupted_operation` — операцию, прерванную перезагрузкой кассы (§7.3):
        по этому полю сервер и админ видят, что обновление не дожило до конца.
        Сессии пользователей наполняются на этапе Э5 (TrayAgent).
        """
        return HealthResponse(
            store_id=config.store_id or None,
            agent_version=APP_VERSION,
            uptime_sec=uptime_sec(),
            work_dir=str(one_c_paths.work_dir),
            base_1c_path=config.base_1c_path,
            os=describe_os(),
            busy=operations.busy_state,
            sessions=[],
            credentials_version=config.credentials_version,
            devices_version=config.devices_version,
            interrupted_operation=operations.interrupted_operation,
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
            work_dir=str(one_c_paths.work_dir),
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
    app.state.agent_paths = one_c_paths
    app.state.agent_provider = process_provider
    app.state.agent_runner = operations
    return app
