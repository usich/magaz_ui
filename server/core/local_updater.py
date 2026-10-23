"""LocalUpdateMonitor — обновление локальных баз 1С напрямую на этой машине (Д-24).

Задачи обновления **локальных** баз (`commands.kind = run_update_db_local`) живут в
той же таблице `commands`, что и операции магазинов, поэтому попадают в единую
историю `/jobs` и журнал. Но их не видит диспетчер магазинов (`list_pending`
исключает команды с `local_base_id`) — их выполняет этот монитор:

* подхватывает `queued`/`running` локальные задачи (`list_pending_local`);
* строит команду 1С теми же хелперами, что и агент (`client.agent.one_c`), но с
  путями платформы и рабочей папки **сервера** (настройки `local_1c_path`,
  `local_update_dir`);
* выполняет `1cv8.exe` через `ProcessProvider` (в тестах подменяется моком);
* пишет прогресс в `job_logs` и закрывает задачу `success`/`failed`.

Ровно одна задача выполняется за один цикл: локальные базы на одной машине не
обновляются параллельно (аналог I-13 агента).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from client.agent.one_c import (
    OneCPaths,
    build_merge_command,
    build_update_db_command,
    log_text_is_effectively_empty,
)
from client.agent.processes import DEFAULT_PROVIDER, ProcessProvider, SubprocessProvider
from server.config import Settings
from server.core.security import SecretBox
from server.db.models import Command, LocalBase
from server.db.session import DatabaseManager
from server.services import command_service, settings_service
from shared.enums import CommandKind, CommandStatus
from shared.schemas import MergeParams, UpdateParams

logger = logging.getLogger(__name__)

__all__ = [
    "LocalUpdateMonitor",
    "build_update_params",
    "build_merge_params",
    "check_files_cycle",
    "merge_cycle",
    "update_db_cycle",
]

STOP_TIMEOUT_SEC = 10
INTERVAL_SETTING = "local_update_interval_sec"
#: Д-25: сколько локальных баз обновлять одновременно (`local_update_concurrency`)
CONCURRENCY_SETTING = "local_update_concurrency"

#: Сообщение успеха обновления БД (перенос v2, §7.1 «менять нельзя»)
SUCCESS_UPDATE_MSG = "Обновление конфигурации успешно завершено"
#: Сообщение успеха слияния (перенос v2, §7.1 «менять нельзя»)
SUCCESS_MERGE_MSG = "Объединение конфигураций успешно завершено"


def build_update_params(base: LocalBase, params: dict[str, Any] | None, *, password: str) -> UpdateParams:
    """Собрать параметры подключения 1С для команды обновления (Д-24).

    Реквизиты берутся из карточки локальной базы (версия 1С, кластер, имя БД,
    логин), пароль расшифровывается SecretBox-ом и в `commands.params` не хранится
    (I-7). Параметры операции — из `command.params`: `is_fast_update`,
    `extension_name`, `is_extension`.
    """
    params = params or {}
    return UpdateParams(
        server_name=base.cluster_name,
        base_name=base.database_name,
        version_1c=base.version_1c,
        user_name=base.user_name_1c,
        password=password,
        is_fast_update=bool(params.get("is_fast_update")),
        extension_name=(params.get("extension_name") or base.extension_name) or None,
        is_extension=params.get("is_extension"),
    )

def build_merge_params(base: LocalBase, params: dict[str, Any] | None, *, password: str) -> MergeParams:
    """Собрать параметры слияния конфигурации/расширения локальной базы (Д-26).

    Реквизиты — из карточки локальной базы, пароль расшифровывается SecretBox-ом
    и в `commands.params` не хранится (I-7). Тип слияния (`is_extension`) и имя
    файла — из `command.params`.
    """
    params = params or {}
    return MergeParams(
        server_name=base.cluster_name,
        base_name=base.database_name,
        version_1c=base.version_1c,
        user_name=base.user_name_1c,
        password=password,
        file_name=str(params.get("file_name") or "update"),
        full_merge=bool(params.get("full_merge")),
        extension_name=(params.get("extension_name") or base.extension_name) or None,
        is_extension=params.get("is_extension"),
        settings_file_name=params.get("settings_file_name"),
    )


async def merge_cycle(
    up: MergeParams,
    paths: OneCPaths,
    *,
    provider: ProcessProvider,
) -> tuple[CommandStatus, str]:
    """Выполнить слияние конфигурации/расширения локальной базы (Д-26, FR-3/FR-4).

    Одна команда `/MergeCfg <файл> [-Extension <имя>] -force -Settings <файл>.xml`
    (аналог шага «слияние» у магазина). Итог — по содержимому лога слияния (I-4).
    """
    from client.agent import one_c  # локально: импорт хелперов не тормозит старт

    log_path = paths.merge_log(up.extension_name)
    await one_c.clear_log_file(log_path)
    args = build_merge_command(up, log_path, paths)
    await one_c.run_1c_command(args, paths.log_executor, provider=provider)

    state, message = await one_c.check_log_file(log_path, SUCCESS_MERGE_MSG)
    if state == one_c.LOG_STATE_SUCCESS:
        return CommandStatus.SUCCESS, SUCCESS_MERGE_MSG
    if state == one_c.LOG_STATE_FAILED:
        tail = (message or "")[-500:]
        return CommandStatus.FAILED, f"Слияние локальной базы не выполнено: {tail}"
    if state == one_c.LOG_STATE_NOT_FOUND:
        return CommandStatus.FAILED, "Лог слияния не создан — запуск 1С не состоялся"
    return CommandStatus.ERROR, f"Слияние не завершилось: {message}"


def check_files_cycle(base: LocalBase, file_name: str, paths: OneCPaths) -> tuple[CommandStatus, str]:
    """Проверить наличие файлов обновления локальной базы в `local_update_dir` (Д-26).

    Аналог `/check-files` магазина, но без сети: файлы `.cf/.cfe/.xml` смотрятся
    прямо в рабочей папке сервера. Не требует запуска `1cv8.exe`.
    """
    from client.agent import one_c

    stem = one_c.split_merge_file_name(file_name)[0]
    names: dict[str, str] = {
        ".cf": f"{stem}.cf",
        ".cfe": f"{stem}.cfe",
        ".xml": f"{stem}.xml",
    }
    found: list[str] = []
    for suffix, name in names.items():
        if paths.update_file(stem, suffix).is_file():
            found.append(name)
    if found:
        return CommandStatus.SUCCESS, f"Файлы «{stem}» найдены: {', '.join(found)}"
    return CommandStatus.FAILED, f"Файлы «{stem}» не найдены в {paths.work_dir}"


async def update_db_cycle(
    up: UpdateParams,
    paths: OneCPaths,
    *,
    provider: ProcessProvider,
    log_update: Path,
) -> tuple[CommandStatus, str]:
    """Выполнить цикл обновления БД (полный или быстрый) и вернуть итог.

    Полный цикл (`is_fast_update=false`): завершение сеансов 1С → `/UpdateDBCfg` →
    разрешение работы пользователей. Быстрый — только `/UpdateDBCfg`. Возвращает
    `(статус, текст для журнала)`.
    """
    from client.agent import one_c  # локально: импорт хелперов не тормозит старт

    steps: list[tuple[str, Path, float | None]] = []
    if not up.is_fast_update:
        steps.append(("завершение сеансов", paths.log_shutdown_users, one_c.USERS_SHUTDOWN_TIMEOUT))
        steps.append(("обновление конфигурации БД", log_update, None))
        steps.append(("разрешение работы пользователей", paths.log_unlock_users, one_c.USERS_UNLOCK_TIMEOUT))
    else:
        steps.append(("обновление конфигурации БД (быстрое)", log_update, None))

    for label, log_path, timeout in steps:
        await one_c.clear_log_file(log_path)
        if label.startswith("завершение"):
            args = one_c.build_shutdown_users_command(up, log_path, paths)
        elif label.startswith("обновление"):
            args = build_update_db_command(up, log_path, paths)
        else:
            args = one_c.build_enable_users_command(up, log_path, paths)
        code = await one_c.run_1c_command(args, paths.log_executor, timeout=timeout, provider=provider)

        if label.startswith("обновление"):
            # FR-5 (перенос v2): `/UpdateDBCfg` может завершиться успешно, но не
            # записать текст в `/Out` — тогда дополняем строкой успеха сами.
            if code is not None and log_path.exists():
                text = log_path.read_text(encoding="utf-8", errors="ignore")
                if log_text_is_effectively_empty(text):
                    log_path.write_text(SUCCESS_UPDATE_MSG, encoding="utf-8")
        logger.debug("Шаг %s завершён (код %s)", label, code)

    # Итог — по содержимому лога обновления (I-4), а не по коду возврата
    state, message = await one_c.check_log_file(log_update, SUCCESS_UPDATE_MSG)
    if state == one_c.LOG_STATE_SUCCESS:
        return CommandStatus.SUCCESS, SUCCESS_UPDATE_MSG
    if state == one_c.LOG_STATE_FAILED:
        tail = (message or "")[-500:]
        return CommandStatus.FAILED, f"Обновление локальной базы не выполнено: {tail}"
    if state == one_c.LOG_STATE_NOT_FOUND:
        return CommandStatus.FAILED, "Лог обновления не создан — запуск 1С не состоялся"
    return CommandStatus.ERROR, f"Обновление не завершилось: {message}"


class LocalUpdateMonitor:
    """Фоновая задача выполнения обновлений локальных баз (паттерн Backup Monitor)."""

    def __init__(
        self,
        db: DatabaseManager,
        settings: Settings,
        *,
        secret_box: SecretBox,
        provider: ProcessProvider = DEFAULT_PROVIDER,
        interval_override: int | None = None,
        concurrency_override: int | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._secret_box = secret_box
        self._provider = provider
        self._interval_override = interval_override
        self._concurrency_override = concurrency_override
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_processed = 0

    @property
    def interval_sec(self) -> int:
        if self._interval_override is not None:
            return max(1, int(self._interval_override))
        with self._db.session() as session:
            return max(1, settings_service.get_int(session, INTERVAL_SETTING))

    @property
    def concurrency(self) -> int:
        """Сколько локальных баз обновлять одновременно (Д-25, `1cv8.exe` параллельно)."""
        if self._concurrency_override is not None:
            return max(1, int(self._concurrency_override))
        with self._db.session() as session:
            return max(1, settings_service.get_int(session, CONCURRENCY_SETTING))

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _paths(self) -> OneCPaths | None:
        base = (self._settings.local_1c_path or "").strip()
        if not base:
            return None
        return OneCPaths(
            work_dir=self._settings.local_update_dir,
            base_1c_path=Path(base),
        )

    async def poll_once(self) -> int:
        """Выполнить один цикл: до `concurrency` незавершённых локальных задач (Д-25).

        Пока базы обновляются параллельно (по настройке `local_update_concurrency`),
        остальные ждут в очереди `queued` и подхватятся следующим циклом.
        """
        paths = self._paths()
        if paths is None:
            return 0
        limit = self.concurrency
        with self._db.session() as session:
            pending = command_service.list_pending_local(session)
            command_ids = [command.id for command in pending[:limit]]
        if not command_ids:
            return 0
        # Каждая задача — в своей asyncio-задаче и своей SQLAlchemy-сессии: конкурентное
        # выполнение `1cv8.exe` не делит ни соединение, ни транзакцию.
        await asyncio.gather(*(self._run_one(cmd_id, paths) for cmd_id in command_ids))
        self.last_processed = len(command_ids)
        return len(command_ids)

    async def _run_one(self, command_id: int, paths: OneCPaths) -> None:
        """Выполнить одну локальную задачу по её id в собственной сессии (Д-26).

        Вид задачи решает, что делать: проверка файлов (`check_files_local`),
        слияние (`run_merge_local`) или обновление БД (`run_update_db_local`).
        """
        with self._db.session() as session:
            command = command_service.get_command(session, command_id)
            if command is None:
                return
            base = command.local_base
            if base is None or not base.is_active:
                command_service.add_log(session, command, "Локальная база не найдена или отключена")
                command_service.set_status(session, command, CommandStatus.FAILED)
                session.commit()
                return

            password = self._secret_box.decrypt(base.password_enc) if base.password_enc else ""
            # RUNNING: `set_status` проставит `started_at`, чтобы UI видел «идёт N сек»
            command_service.set_status(session, command, CommandStatus.RUNNING)
            command_service.add_log(session, command, f"Запуск {self._label(command)} «{base.name}»")
            # Коммитим статус до долгой операции 1С — UI успевает показать прогресс
            session.commit()

            if command.kind == str(CommandKind.CHECK_FILES_LOCAL):
                file_name = str((command.params or {}).get("file_name") or "update")
                status, message = check_files_cycle(base, file_name, paths)
            elif command.kind == str(CommandKind.RUN_MERGE_LOCAL):
                up = build_merge_params(base, command.params, password=password)
                status, message = await merge_cycle(up, paths, provider=self._provider)
            else:
                up = build_update_params(base, command.params, password=password)
                status, message = await update_db_cycle(
                    up, paths, provider=self._provider, log_update=paths.log_update_db
                )

            if status == CommandStatus.SUCCESS:
                command_service.add_log(session, command, message)
            else:
                command_service.add_log(session, command, message, level="error")
            command_service.set_status(session, command, status)
            session.commit()

    @staticmethod
    def _label(command: Command) -> str:
        """Человекочитаемое название задачи для журнала (Д-26)."""
        labels = {
            str(CommandKind.CHECK_FILES_LOCAL): "проверку файлов локальной базы",
            str(CommandKind.RUN_MERGE_LOCAL): "слияние конфигурации локальной базы",
            str(CommandKind.RUN_UPDATE_DB_LOCAL): "обновление локальной базы",
        }
        return labels.get(command.kind, f"задачу {command.kind}")

    async def run(self) -> None:
        await asyncio.sleep(max(0.0, float(self._settings.local_updater_startup_delay_sec)))
        while not self._stop_event.is_set():
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 - цикл не должен упасть из-за одной задачи
                logger.exception("Сбой цикла обновления локальных баз")
            await self._sleep(float(self.interval_sec))
        logger.debug("LocalUpdateMonitor остановлен")

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=max(0.0, seconds))
        except (TimeoutError, asyncio.TimeoutError):
            pass

    def start(self) -> asyncio.Task[None]:
        if self.is_running and self._task is not None:
            return self._task
        self._stop_event.clear()
        self._task = asyncio.create_task(self.run(), name="magaz-local-updater")
        logger.info("LocalUpdateMonitor запущен: интервал %s с", self.interval_sec)
        return self._task

    async def stop(self) -> None:
        self._stop_event.set()
        task, self._task = self._task, None
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(task, timeout=STOP_TIMEOUT_SEC)
        except (TimeoutError, asyncio.TimeoutError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

