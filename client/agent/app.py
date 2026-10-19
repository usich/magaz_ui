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
сервера операцию, прерванную перезагрузкой кассы (§7.3), `POST /credentials`
(Э4, П-1) — приём учётных данных 1С от сервера, и `POST /devices/config` +
`POST /devices/check-now` (Э4Б, Д-1) — список оборудования подсети и
внеочередная проверка по кнопке «Проверить сейчас».

Э5 (ФН3) добавляет уведомления: `POST /notify` принимает уведомление от сервера
и кладёт его в файловую очередь (§7.4), а локальные точки `127.0.0.1` —
`GET /notifications/pending`, `POST /notifications/{id}/shown|ack|snooze` —
обслуживают TrayAgent пользовательской сессии. Токен агента в сессию
пользователя не попадает: TrayAgent не ходит в сеть (§7.4). Кнопка «Отложить» —
П-2/Д-10, интервал возврата задаёт сервер (`notification_snooze_min`, §4.9).

Авторизация: `X-Agent-Token` из конфига магазина плюс, если задан
`allowed_server_ips`, проверка IP источника (§5.1, §8.1). Повторный запуск
1С-операции, пока идёт предыдущая, отвечает `409 Conflict` (I-13, Q23); лог
обнуляется до постановки задачи в очередь (I-3). Пароль 1С приходит только
push-ом, шифруется Fernet-ом с ключом под DPAPI машины и в логи не попадает
(П-1, I-7). Опрос оборудования подсети ведёт фоновая задача `DeviceMonitor`
(§7.6): `icmp` через `ping.exe` (argv, без shell) либо TCP-connect, результат
батчем уходит на сервер. Уведомления ведёт `NotificationMonitor` (§7.4):
сессии, дедлайны, дублирующий `msg.exe` и досылка подтверждений.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from client.agent import credentials, devices, downloader, messenger, notifications
from client.agent.config import AgentConfig, describe_os
from client.agent.executor import OperationBusyError, OperationRunner
from client.agent.models import AgentVersionResponse
from client.agent.one_c import (
    LOG_STATE_FAILED,
    LOG_STATE_SUCCESS,
    OneCPaths,
    SUCCESS_MERGE_MSG,
    SUCCESS_UPDATE_MSG,
    UpdateFileError,
    check_log_file,
    delete_update_files,
)
from client.agent.processes import DEFAULT_PROVIDER, ProcessProvider
from shared.schemas import (
    AckRequest,
    CheckFilesParams,
    CheckFilesResponse,
    CredentialsPush,
    DeleteFileParams,
    DeleteFileResponse,
    DeviceCheckRequest,
    DeviceCheckResult,
    DevicesConfigPush,
    DownloadParams,
    HealthResponse,
    JobStateResponse,
    MergeParams,
    MessageParams,
    NotifyRequest,
    SessionRef,
    SnoozeRequest,
    UpdateParams,
)
from shared.timeutil import to_iso_z, utcnow
from shared.version import APP_VERSION

logger = logging.getLogger(__name__)

__all__ = ["LOOPBACK_HOSTS", "create_agent_app"]

#: Хосты, которым доступны локальные точки уведомлений (§7.4).
#: `testclient` — адрес клиента в тестах FastAPI: в бою такого узла не бывает.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})



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
    devices_monitor: bool | None = None,
    devices_transport: Any = None,  # noqa: ANN401 - httpx.AsyncBaseTransport в тестах
    devices_startup_delay_sec: float = 5.0,
    notifications_monitor: bool | None = None,
    notifications_transport: Any = None,  # noqa: ANN401 - httpx.AsyncBaseTransport в тестах
    notifications_startup_delay_sec: float = 5.0,
) -> FastAPI:
    """Собрать приложение агента по его конфигу (§7.2).

    `started_at` передаётся явно в тестах, чтобы `uptime_sec` был предсказуем;
    `paths`, `provider`, `runner` — точки подмены для проверок (§7.5, §13):
    реальная 1С, `msg.exe` и `taskkill` в тестах не запускаются.

    Э4Б: `devices_monitor` включает фоновый опрос оборудования подсети (§7.6).
    Э5: `notifications_monitor` включает расписание уведомлений (§7.4) —
    сессии, дедлайны, дублирующий `msg.exe` и досылка подтверждений. Оба
    расписания по умолчанию поднимаются, если в конфиге задан адрес сервера;
    `False` нужен тестам и локальной отладке без сети.
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
    #: П-1: профиль 1С агента — расшифровка пароля и приём push-а `/credentials`
    profile_store = credentials.OneCProfileStore(
        config.one_c_profile,
        config_path=config_path,
    )
    #: Д-1: список оборудования подсети — копия на диске переживает перезапуск
    devices_store = devices.DevicesStore.load(
        config.devices_path,
        interval_sec=config.device_check_interval_sec,
        timeout_ms=config.device_ping_timeout_ms,
    )
    if devices_store.has_snapshot:
        # Принятый ранее список — источник истины для `/health`: иначе после
        # перезапуска службы агент докладывал бы версию из agent-config.json (§7.6)
        config.devices_version = devices_store.version
    device_monitor = devices.DeviceMonitor(
        devices_store,
        checker=devices.DeviceChecker(
            provider=process_provider, timeout_ms=config.device_ping_timeout_ms
        ),
        sender=devices.DeviceStatusSender(config, transport=devices_transport),
        store_id=config.store_id,
        startup_delay_sec=devices_startup_delay_sec,
    )
    monitor_enabled = (
        bool(config.server_url) if devices_monitor is None else bool(devices_monitor)
    )
    #: Э5 (ФН3): очередь уведомлений и подтверждений — файл `notifications.json`
    notifications_queue = notifications.NotificationQueue.load(config.notifications_path)
    receipt_sender = notifications.ReceiptSender(config, transport=notifications_transport)
    notification_monitor = notifications.NotificationMonitor(
        notifications_queue,
        config=config,
        sender=receipt_sender,
        provider=process_provider,
        startup_delay_sec=notifications_startup_delay_sec,
    )
    notifications_enabled = (
        bool(config.server_url)
        if notifications_monitor is None
        else bool(notifications_monitor)
    )
    #: Э9: кэш картинок тела уведомлений рядом с очередью (`notifications-images/`)
    image_cache = notifications.NotificationImageCache(
        Path(config.notifications_path).parent / notifications.IMAGE_CACHE_DIR_NAME
    )
    image_downloader = notifications.ImageDownloader(
        config, cache=image_cache, transport=notifications_transport
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Старт: помечаем прерванную операцию; остановка: гасим воркеры (§7.3)."""
        interrupted = operations.mark_interrupted()
        logger.info(
            "MagazAgent запущен: магазин %s, рабочая папка %s, прерванная операция: %s",
            config.store_id or "—",
            one_c_paths.work_dir,
            interrupted.operation if interrupted else "нет",
        )
        if monitor_enabled:
            device_monitor.start()
            logger.info(
                "Опрос оборудования подсети включён: устройств %s (активных %s), интервал %s с",
                len(devices_store.config.devices),
                len(devices_store.active()),
                device_monitor.interval_sec,
            )
        if notifications_enabled:
            notification_monitor.start()
            logger.info(
                "Уведомления включены: у очереди %s уведомлений, интервал сверки %s с",
                len(notifications_queue.notifications),
                notification_monitor.interval_sec,
            )
        try:
            yield
        finally:
            if notifications_enabled:
                await notification_monitor.stop()
            await device_monitor.stop()
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

    def require_loopback(request: Request) -> None:
        """Локальные точки уведомлений: только `127.0.0.1` (§7.4).

        TrayAgent не имеет токена агента и не должен иметь: он разговаривает со
        службой по локальному адресу. Поэтому здесь проверяется адрес источника,
        а не `X-Agent-Token` — так токен не попадает в пользовательскую сессию.
        """
        host = source_host(request)
        if host not in LOOPBACK_HOSTS:
            logger.warning(
                "Отклонён запрос к локальным точкам уведомлений с адреса %s", host
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Локальные точки уведомлений доступны только с 127.0.0.1",
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
        "/files/delete",
        response_model=DeleteFileResponse,
        dependencies=[Depends(require_agent_token)],
        summary="Удалить файлы обновления на кассе (Э10)",
        tags=["1c"],
    )
    async def delete_files(params: DeleteFileParams = Body(...)) -> DeleteFileResponse:
        """Удалить `.cf`/`.cfe`/`.xml` из рабочей папки агента (Э10, §11.3).

        Удаляются только файлы обновления: имя проверяется по I-2 (без разделителей
        пути), расширение — из `.cf/.cfe/.xml`. Логи 1С и файлы базы не задеваются.
        Снимок `file_checks` сервер обновит следующей проверкой («Проверить»).
        """
        try:
            removed, missing = delete_update_files(
                one_c_paths, file_name=params.file_name, suffix=params.suffix
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        except UpdateFileError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        logger.info(
            "Удаление файлов «%s»: удалено %s, не было %s",
            params.file_name,
            removed,
            missing,
        )
        return DeleteFileResponse(
            removed=removed, missing=missing, work_dir=str(one_c_paths.work_dir)
        )

    @app.post(
        "/credentials",
        dependencies=[Depends(require_agent_token)],
        summary="Приём учётных данных 1С от сервера (П-1)",
        tags=["agent"],
    )
    def push_credentials(payload: CredentialsPush) -> dict[str, object]:
        """Сохранить логин и пароль 1С из push-а `POST /credentials` (§5.1.2, §8.3).

        Пароль шифруется Fernet-ом с ключом под DPAPI этой машины и попадает
        только в `agent-config.json` (`one_c_profile.password_enc`). В логах,
        ответе и `/health` значения пароля нет — наружу отдаётся версия профиля
        (I-7), поэтому после приёма `/health` сразу докладывает новую версию и
        сервер перестаёт считать учётные данные устаревшими.
        """
        try:
            profile = profile_store.apply_push(payload)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        config.one_c_profile = profile
        return {
            "stored": True,
            "credentials_version": profile.credentials_version,
            "password_set": bool(profile.password_enc),
        }

    @app.post(
        "/devices/config",
        dependencies=[Depends(require_agent_token)],
        summary="Приём списка оборудования подсети от сервера (Д-1)",
        tags=["devices"],
    )
    def push_devices_config(payload: DevicesConfigPush) -> dict[str, object]:
        """Сохранить список оборудования и расписание опроса (§5.1.2, §7.6).

        Копия ложится в `devices.json` рядом с конфигом: опрос продолжается,
        когда сервер недоступен. Версия из push-а сразу видна в `/health` —
        сервер перестаёт считать список оборудования расходящимся.
        """
        stored = devices_store.apply_push(payload)
        config.devices_version = stored.devices_version
        return {
            "stored": True,
            "devices_version": stored.devices_version,
            "devices": len(stored.devices),
            "active": len(stored.active()),
            "check_interval_sec": stored.check_interval_sec,
        }

    @app.post(
        "/devices/check-now",
        response_model=list[DeviceCheckResult],
        dependencies=[Depends(require_agent_token)],
        summary="Внеочередная проверка оборудования (Д-1)",
        tags=["devices"],
    )
    async def check_devices_now(payload: DeviceCheckRequest) -> list[DeviceCheckResult]:
        """Проверить указанные IP и сразу вернуть результат (§5.1.2).

        Кнопка «Проверить сейчас» в дашборде: адреса берёт сервер из
        `store_devices` и записывает ответ сам — подсеть агент не сканирует (§7.6).
        """
        return await device_monitor.check_now(payload.devices)

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
        prepared = profile_store.prepare(params)
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
        prepared = profile_store.prepare(params)
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

    # --- Э5 (ФН3): уведомления (§5.1.2, §7.4) --------------------------------

    @app.post(
        "/notify",
        dependencies=[Depends(require_agent_token)],
        summary="Приём уведомления от сервера",
        tags=["notifications"],
    )
    async def notify(payload: NotifyRequest = Body(...)) -> dict[str, object]:
        """Положить уведомление в файловую очередь и вернуть число сессий (§5.1.2).

        Показ делает TrayAgent: служба только хранит уведомление, поэтому
        недоступность сервера после этого запроса ничего не ломает — окно
        появится, когда пользователь войдёт (§7.4).
        """
        result = notifications_queue.accept(payload)
        # Э9: картинки тела скачиваются фоном — касса не ждёт сеть, а окно покажет их
        # из локального кэша, когда пользователь откроет уведомление (§7.4)
        if payload.images:
            spawn(
                image_downloader.fetch(int(payload.id), list(payload.images)),
                name="agent-notify-images",
            )
        return result

    @app.get(
        "/notifications/pending",
        dependencies=[Depends(require_loopback)],
        summary="Уведомления для сессии пользователя",
        tags=["notifications"],
    )
    def notifications_pending(
        session: str = Query(min_length=1, description="Идентификатор сессии Windows"),
        user: str = Query(min_length=1, description="Логин Windows в сессии"),
    ) -> dict[str, object]:
        """Что показать в этой сессии (§5.1.2, §7.4).

        Отдаются только незакрытые уведомления: отложенное вернётся само, когда
        пройдёт `snooze_until` (П-2), а `info` ждёт явного «Ознакомлен» в трее.
        Заодно служба запоминает сессию — по ней сервер считает «X из Y» (§4.8).
        """
        ref = SessionRef(session_id=session, session_user=user)
        items = notifications_queue.pending_for(ref)
        return {"notifications": [item.model_dump(mode="json") for item in items]}

    @app.get(
        "/notifications/archive",
        dependencies=[Depends(require_loopback)],
        summary="Прочитанные уведомления — справочник окна кассы",
        tags=["notifications"],
    )
    def notifications_archive(
        session: str = Query(min_length=1, description="Идентификатор сессии Windows"),
        user: str = Query(min_length=1, description="Логин Windows в сессии"),
    ) -> dict[str, object]:
        """Что остаётся на кассе после «Ознакомлен» (`delete_after_read=false`, §7.4, Э9).

        Вкладка «Справочник» в окне уведомлений: прочитанное не исчезает, если админ
        так решил, — кассир возвращается к инструкции, когда нужно.
        """
        ref = SessionRef(session_id=session, session_user=user)
        items = notifications_queue.archive_for(ref)
        return {"notifications": [item.model_dump(mode="json") for item in items]}

    @app.get(
        "/notifications/{notification_id}/images/{name}",
        dependencies=[Depends(require_loopback)],
        summary="Картинка тела уведомления из локального кэша",
        tags=["notifications"],
    )
    def notification_image(notification_id: int, name: str) -> Response:
        """Отдать картинку уведомления из кэша кассы (§7.4, Э9).

        Трей рисует окно без сети: картинка пришла один раз с сервера (агент качает
        её фоном при `POST /notify`) и с тех пор лежит файлом рядом с очередью.
        """
        data = image_cache.read(notification_id, name)
        if data is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"Картинка {name!r} уведомления id={notification_id} "
                    "на кассе не найдена"
                ),
            )
        return Response(
            content=data,
            media_type=notifications.image_media_type(name),
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @app.post(
        "/notifications/{notification_id}/shown",
        dependencies=[Depends(require_loopback)],
        summary="Уведомление показано (тост)",
        tags=["notifications"],
    )
    async def notification_shown(
        notification_id: int, payload: SessionRef = Body(...)
    ) -> dict[str, object]:
        """Отметить «показано» и сразу попробовать дослать подтверждение (§7.4).

        Досылка идёт фоновой задачей: TrayAgent не должен ждать сервер, а при
        недоступности сети подтверждение останется в очереди на диске.
        """
        if not notifications_queue.mark_shown(notification_id, payload):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Уведомление id={notification_id} на кассе не найдено",
            )
        spawn(notification_monitor.flush(), name="agent-receipts")
        return {"ok": True, "status": "shown"}

    @app.post(
        "/notifications/{notification_id}/ack",
        dependencies=[Depends(require_loopback)],
        summary="«Ознакомлен»",
        tags=["notifications"],
    )
    async def notification_ack(
        notification_id: int, payload: AckRequest = Body(...)
    ) -> dict[str, object]:
        """Кнопка «Ознакомлен» (FR-26): фиксируется по сессии Windows.

        Учёт именно по сессии, а не по сотруднику (П-6): справочника нет, и
        «ознакомлен» ставит тот, кто в этот момент работает за кассой (§11.5).
        """
        ref = SessionRef(session_id=payload.session_id, session_user=payload.session_user)
        if not notifications_queue.acknowledge(
            notification_id, ref, comment=payload.comment
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Уведомление id={notification_id} на кассе не найдено",
            )
        spawn(notification_monitor.flush(), name="agent-receipts")
        return {"ok": True, "status": "acknowledged"}

    @app.post(
        "/notifications/{notification_id}/snooze",
        dependencies=[Depends(require_loopback)],
        summary="«Отложить»",
        tags=["notifications"],
    )
    async def notification_snooze(
        notification_id: int, payload: SnoozeRequest = Body(...)
    ) -> dict[str, object]:
        """Кнопка «Отложить» (П-2, Д-10).

        Уведомление остаётся **непрочитанным** и скрывается до `snooze_until`:
        интервал возврата задан настройкой сервера (`notification_snooze_min`)
        и приехал вместе с уведомлением — на кассе его не правят.
        """
        ref = SessionRef(session_id=payload.session_id, session_user=payload.session_user)
        until = notifications_queue.snooze(notification_id, ref)
        if until is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Уведомление id={notification_id} на кассе не найдено",
            )
        spawn(notification_monitor.flush(), name="agent-receipts")
        return {"ok": True, "status": "snoozed", "snooze_until": until}

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
        Сессии пользователей берутся из очереди уведомлений: их видит TrayAgent и
        сообщает службе при опросе `GET /notifications/pending` (§7.4, Q50).
        """
        return HealthResponse(
            store_id=config.store_id or None,
            agent_version=APP_VERSION,
            uptime_sec=uptime_sec(),
            work_dir=str(one_c_paths.work_dir),
            base_1c_path=config.base_1c_path,
            os=describe_os(),
            busy=operations.busy_state,
            sessions=notifications_queue.sessions(),
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
    #: Д-1: расписание опроса оборудования (диагностика и тесты)
    app.state.agent_devices = device_monitor
    #: Э5: расписание уведомлений и очередь подтверждений (диагностика и тесты)
    app.state.agent_notifications = notification_monitor
    app.state.agent_notification_queue = notifications_queue
    app.state.agent_image_cache = image_cache
    return app
