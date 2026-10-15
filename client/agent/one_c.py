"""Ядро 1С на кассе: сборка команд, логи, запуск без shell (§5.1.1, FR-3…FR-10).

Поведение v2 переносится 1:1: те же ключи команд (`/MergeCfg`, `-force`, `-Settings`,
`/UpdateDBCfg`, `/UC…`, `/WA-`, `/AU-`), те же имена лог-файлов (§7.1: «менять
нельзя»), те же состояния `success/failed/in_progress/not_found` и тексты
`/check-*`. Отличия ровно те, что требует спецификация:

* путь к платформе и рабочая папка приходят из конфига агента (I-14) — третьим
  аргументом `paths`, а не глобальными константами;
* пароль маскируется в `format_command()` (I-7) — в лог попадает `/P***`;
* внешние процессы запускает провайдер (§7.5, `client/agent/processes.py`),
  поэтому в тестах 1С, `msg.exe` и `taskkill` подменяются моками.

Инварианты, за которые отвечает этот модуль: I-1 (только argv, никакого shell),
I-3 (лог обнуляется до запуска), I-4 (статус — по содержимому лога),
I-5 (`/DisableStartupMessages`), I-6 (вывод вычитывается и пишется в
`log-1cv8-console.txt`), I-7 (пароль не попадает в логи).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from client.agent.processes import DEFAULT_PROVIDER
from shared.paths import UPDATE_FILE_SUFFIXES, validate_file_name

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    from client.agent.processes import ProcessProvider
    from shared.schemas import Base1CParams, MergeParams, UpdateParams

logger = logging.getLogger(__name__)


__all__ = [
    "CMD_ENABLE_USERS",
    "CMD_SHUTDOWN_USERS",
    "ERROR_MARKERS",
    "LOG_EXECUTOR_FILE_NAME",
    "LOG_MERGE_EXTENSION_FILE_NAME",
    "LOG_MERGE_FILE_NAME",
    "LOG_SHUTDOWN_USERS_FILE_NAME",
    "LOG_STATE_FAILED",
    "LOG_STATE_IN_PROGRESS",
    "LOG_STATE_NOT_FOUND",
    "LOG_STATE_SUCCESS",
    "LOG_UNLOCK_USERS_FILE_NAME",
    "LOG_UPDATE_DB_EXTENSION_FILE_NAME",
    "LOG_UPDATE_DB_FILE_NAME",
    "MERGE_PERMISSION_CODE",
    "SUCCESS_MERGE_MSG",
    "SUCCESS_UPDATE_MSG",
    "UPDATE_PERMISSION_CODE",
    "USERS_SHUTDOWN_TIMEOUT",
    "USERS_UNLOCK_TIMEOUT",
    "OneCPaths",
    "UpdateFileError",
    "build_enable_users_command",
    "build_merge_command",
    "build_shutdown_users_command",
    "build_update_db_command",
    "check_log_file",
    "clear_log_file",
    "delete_update_files",
    "format_command",
    "mask_command",
    "run_1c_command",
]

# --- Возможные состояния процесса по лог-файлу 1С (перенос v2) ----------------
LOG_STATE_SUCCESS = "success"
LOG_STATE_FAILED = "failed"
LOG_STATE_IN_PROGRESS = "in_progress"
LOG_STATE_NOT_FOUND = "not_found"

# --- Строки для парсинга логов (перенос v2: менять нельзя, I-4) --------------
SUCCESS_MERGE_MSG = "Объединение конфигураций успешно завершено"
SUCCESS_UPDATE_MSG = "Обновление конфигурации успешно завершено"
ERROR_MARKERS = ("Ошибка", "Error")

# --- Коды разрешения (ключи блокировки информационной базы) ------------------
MERGE_PERMISSION_CODE = "ПакетноеОбновлениеКонфигурацииИБ"
UPDATE_PERMISSION_CODE = "КодРазрешения"

# --- Сеансовые параметры ключа /C --------------------------------------------
CMD_SHUTDOWN_USERS = "ЗавершитьРаботуПользователей"
CMD_ENABLE_USERS = "РазрешитьРаботуПользователей"

# --- Таймауты (секунды), перенос v2 -----------------------------------------
USERS_SHUTDOWN_TIMEOUT = 15
USERS_UNLOCK_TIMEOUT = 60

# --- Имена файлов в рабочей папке (§7.1: имена — как в v2, менять нельзя) ----
LOG_MERGE_FILE_NAME = "log-merge.txt"
LOG_MERGE_EXTENSION_FILE_NAME = "log-merge-extension.txt"
LOG_UPDATE_DB_FILE_NAME = "log-update-db.txt"
LOG_UPDATE_DB_EXTENSION_FILE_NAME = "log-update-db-extension.txt"
LOG_SHUTDOWN_USERS_FILE_NAME = "log-shutdown-users.txt"
LOG_UNLOCK_USERS_FILE_NAME = "log-unlock-users.txt"
LOG_EXECUTOR_FILE_NAME = "log-1cv8-console.txt"
FULL_MERGE_SETTINGS_FILE_NAME = "full_merge.xml"

#: Префикс ключа `/P<пароль>` — единственный ключ 1С, начинающийся с `/P`
PASSWORD_ARG_PREFIX = "/P"
#: Заменитель пароля в логах (I-7)
PASSWORD_MASK = "/P***"


@dataclass(frozen=True, slots=True)
class OneCPaths:
    """Пути агента, от которых зависят команды 1С (I-14: без хардкодов).

    `work_dir` — папка рядом с exe, где лежат `.cf/.cfe/.xml` и логи
    (`get_application_root()` либо `work_dir` из конфига); `base_1c_path` —
    корень установленных платформ 1С из `agent-config.json`.
    """

    work_dir: Path
    base_1c_path: Path

    # --- Логи операций: имена закреплены в §7.1 -----------------------------
    @property
    def log_merge(self) -> Path:
        """`log-merge.txt` — слияние основной конфигурации."""
        return self.work_dir / LOG_MERGE_FILE_NAME

    @property
    def log_merge_extension(self) -> Path:
        """`log-merge-extension.txt` — слияние расширения."""
        return self.work_dir / LOG_MERGE_EXTENSION_FILE_NAME

    @property
    def log_update_db(self) -> Path:
        """`log-update-db.txt` — обновление конфигурации БД."""
        return self.work_dir / LOG_UPDATE_DB_FILE_NAME

    @property
    def log_update_db_extension(self) -> Path:
        """`log-update-db-extension.txt` — обновление расширения."""
        return self.work_dir / LOG_UPDATE_DB_EXTENSION_FILE_NAME

    @property
    def log_shutdown_users(self) -> Path:
        """`log-shutdown-users.txt` — завершение работы пользователей."""
        return self.work_dir / LOG_SHUTDOWN_USERS_FILE_NAME

    @property
    def log_unlock_users(self) -> Path:
        """`log-unlock-users.txt` — снятие блокировки базы."""
        return self.work_dir / LOG_UNLOCK_USERS_FILE_NAME

    @property
    def log_executor(self) -> Path:
        """`log-1cv8-console.txt` — общий stdout/stderr всех запусков 1cv8.exe."""
        return self.work_dir / LOG_EXECUTOR_FILE_NAME

    @property
    def settings_full_merge(self) -> Path:
        """`full_merge.xml` — настройки полного слияния (`full_merge=true`)."""
        return self.work_dir / FULL_MERGE_SETTINGS_FILE_NAME

    # --- Файлы обновления и исполняемый файл платформы ----------------------
    def executable(self, version_1c: str) -> Path:
        """`<base_1c_path>/<version_1c>/bin/1cv8.exe` (§7.1, I-14)."""
        return self.base_1c_path / version_1c / "bin" / "1cv8.exe"

    def update_file(self, file_name: str, suffix: str) -> Path:
        """Файл обновления в рабочей папке: `<file_name><suffix>` (`.cf/.cfe/.xml`)."""
        return self.work_dir / f"{file_name}{suffix}"

    def merge_settings(self, file_name: str, *, full_merge: bool) -> Path:
        """Настройки слияния: `full_merge.xml` либо `<file_name>.xml` (перенос v2)."""
        if full_merge:
            return self.settings_full_merge
        return self.update_file(file_name, ".xml")

    def merge_log(self, extension_name: str | None) -> Path:
        """Лог слияния: у расширения свой файл, иначе — основной (перенос v2)."""
        return self.log_merge_extension if extension_name else self.log_merge

    def update_log(self, extension_name: str | None) -> Path:
        """Лог обновления БД: у расширения свой файл, иначе — основной."""
        return self.log_update_db_extension if extension_name else self.log_update_db


def mask_command(args: list[str]) -> list[str]:
    """Скрыть пароль в аргументах команды перед записью в лог (I-7)."""
    return [PASSWORD_MASK if arg.startswith(PASSWORD_ARG_PREFIX) else arg for arg in args]


class UpdateFileError(RuntimeError):
    """Файл обновления удалить не удалось: занят, нет прав, каталог только для чтения."""


def delete_update_files(
    paths: OneCPaths, *, file_name: str, suffix: str | None = None
) -> tuple[list[str], list[str]]:
    """Удалить файлы обновления из рабочей папки кассы (Э10, §11.3).

    Возвращает `(удалённые, отсутствовавшие)`. Удаляются только файлы обновления:
    имя проходит `validate_file_name` (без пути и разделителей, I-2), расширение
    берётся из `UPDATE_FILE_SUFFIXES` (`.cf/.cfe/.xml`) — поэтому действием нельзя
    задеть ни `1cv8.exe`, ни логи, ни файлы базы; пустой `suffix` — «все три».

    Логи 1С и снимок `file_checks` это не трогает: после удаления админ может
    нажать «Проверить», чтобы матрица `/files` отразила факт (§11.3).
    """
    name = validate_file_name(file_name)
    suffixes = (suffix,) if suffix else UPDATE_FILE_SUFFIXES
    removed: list[str] = []
    missing: list[str] = []
    for item in suffixes:
        path = paths.update_file(name, item)
        if not path.is_file():
            missing.append(path.name)
            continue
        try:
            path.unlink()
        except OSError as exc:
            raise UpdateFileError(f"Не удалось удалить {path.name}: {exc}") from exc
        removed.append(path.name)
        logger.info("Файл обновления %s удалён из %s", path.name, paths.work_dir)
    return removed, missing


def format_command(args: list[str]) -> str:
    """Человекочитаемое представление команды (только для логов, без пароля)."""
    return subprocess.list2cmdline(mask_command(args))


def _connection_args(params: Base1CParams) -> list[str]:
    """Общие аргументы подключения к информационной базе (перенос v2, I-5)."""
    args = [f"/S{params.server_name}\\{params.base_name}", f"/N{params.user_name}"]
    if params.password:
        args.append(f"/P{params.password}")
    args.append("/DisableStartupMessages")
    return args


def build_merge_command(params: MergeParams, log_path: Path, paths: OneCPaths) -> list[str]:
    """Слияние основной конфигурации (FR-3) или расширения (FR-4).

    Перенос v2 без изменений логики: `/MergeCfg <file>.cf -force -Settings <file>.xml`
    (`full_merge.xml` при `full_merge=true`); для расширения — `/Extension <name>
    /MergeCfg <file>.cfe -force` и **без** `-Settings`.
    """
    args = [str(paths.executable(params.version_1c)), "CONFIG", *_connection_args(params)]

    if params.extension_name:
        source_file = paths.update_file(params.file_name, ".cfe")
        args += ["/Extension", params.extension_name, "/MergeCfg", str(source_file), "-force"]
    else:
        source_file = paths.update_file(params.file_name, ".cf")
        setting_file = paths.merge_settings(params.file_name, full_merge=params.full_merge)
        args += ["/MergeCfg", str(source_file), "-force", "-Settings", str(setting_file)]

    args += [f"/UC{MERGE_PERMISSION_CODE}", "/Out", str(log_path)]
    return args


def build_update_db_command(params: UpdateParams, log_path: Path, paths: OneCPaths) -> list[str]:
    """Обновление конфигурации БД (FR-5): `/UpdateDBCfg /UCКодРазрешения` (перенос v2)."""
    args = [str(paths.executable(params.version_1c)), "CONFIG", *_connection_args(params)]
    if params.extension_name:
        args += ["/Extension", params.extension_name]
    args += ["/UpdateDBCfg", f"/UC{UPDATE_PERMISSION_CODE}", "/Out", str(log_path)]
    return args


def build_shutdown_users_command(
    params: Base1CParams, log_path: Path, paths: OneCPaths
) -> list[str]:
    """Завершение работы пользователей перед обновлением БД (перенос v2, FR-6)."""
    return [
        str(paths.executable(params.version_1c)),
        "ENTERPRISE",
        *_connection_args(params),
        "/WA-",
        "/AU-",
        "-force",
        f"/C{CMD_SHUTDOWN_USERS}",
        "/Out",
        str(log_path),
    ]


def build_enable_users_command(
    params: Base1CParams, log_path: Path, paths: OneCPaths
) -> list[str]:
    """Снятие блокировки базы и разрешение работы пользователей (перенос v2, FR-6)."""
    return [
        str(paths.executable(params.version_1c)),
        "ENTERPRISE",
        *_connection_args(params),
        "-force",
        f"/C{CMD_ENABLE_USERS}",
        f"/UC{UPDATE_PERMISSION_CODE}",
        "/Out",
        str(log_path),
    ]


# --- Логи 1С и запуск процессов (FR-7, FR-10) -------------------------------


def _truncate_log(log_path: Path) -> None:
    """Создать пустой лог-файл (рабочая папка создаётся, если её нет)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")


def _read_log_text(log_path: Path) -> str:
    """Прочитать лог 1С: битые символы кодировки не должны ломать статус."""
    return log_path.read_text(encoding="utf-8", errors="ignore")


async def clear_log_file(log_path: Path) -> None:
    """Обнулить (или создать) лог-файл **до** запуска процесса (I-3).

    Нужно, чтобы `/check-*` не отдал результат предыдущего запуска: «залипший
    успех» после перезагрузки службы — худший сценарий для админа (§14.2, R5).
    """
    await asyncio.to_thread(_truncate_log, log_path)


async def check_log_file(log_path: Path, success_message: str) -> tuple[str, str]:
    """Состояние процесса по лог-файлу: пара (состояние, содержимое/пояснение).

    Перенос v2 без изменений логики (I-4): статус берётся **только** из
    содержимого лога, код возврата `1cv8.exe` на него не влияет.
    """
    if not await asyncio.to_thread(log_path.exists):
        return LOG_STATE_NOT_FOUND, "Log file not found."

    content = await asyncio.to_thread(_read_log_text, log_path)

    if not content.strip():
        return LOG_STATE_IN_PROGRESS, "Log file is empty or process is not started yet."
    if success_message in content:
        return LOG_STATE_SUCCESS, content
    if any(marker in content for marker in ERROR_MARKERS):
        return LOG_STATE_FAILED, content
    return LOG_STATE_IN_PROGRESS, "Process in progress... " + content


async def run_1c_command(
    args: list[str],
    log_path: Path | None = None,
    timeout: float | None = None,
    *,
    provider: ProcessProvider | None = None,
) -> int | None:
    """Запустить команду 1С без командной оболочки (I-1) и дождаться её конца.

    Аргументы передаются списком, поэтому параметры запроса не могут превратиться
    в дополнительные команды. Пароль в лог не попадает: команда печатается через
    `format_command()` (I-7). Возвращает код возврата либо `None` — сработал `timeout`.
    """
    logger.info("Запуск команды: %s", format_command(args))
    active = provider or DEFAULT_PROVIDER
    return await active.run_captured(args, log_path=log_path, timeout=timeout)
