"""Конфигурация агента кассы (§7.1, §7.2).

Агент читает единственный файл `agent-config.json` (по умолчанию —
`C:\\MagazAgent\\agent-config.json`). Все пути вычисляются от места запуска
exe (`get_application_root()`), абсолютных путей в логике нет (I-14); значения
ниже — только значения по умолчанию из спецификации, их переопределяет конфиг.

Инвариант I-10: импорт модуля ничего не читает и не создаёт — конфиг читается
явной функцией `load_config()`.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

__all__ = [
    "AGENT_CONFIG_FILE_NAME",
    "AGENT_DEVICES_FILE_NAME",
    "AGENT_NOTIFICATIONS_FILE_NAME",
    "AGENT_STATE_FILE_NAME",
    "CONFIG_PATH_ENV",
    "DEFAULT_AGENT_PORT",
    "DEFAULT_BASE_1C_PATH",
    "DEFAULT_DEVICE_CHECK_INTERVAL_SEC",
    "DEFAULT_DEVICE_PING_TIMEOUT_MS",
    "DEFAULT_FILE_NAME",
    "DEFAULT_LOG_DIR",
    "DEFAULT_WORK_DIR",
    "LOG_DIR_ENV",
    "AgentConfig",
    "OneCProfile",
    "default_config_path",
    "describe_os",
    "get_application_root",
    "load_config",
    "save_one_c_profile",
]

#: Значения по умолчанию §7.1/§7.2 (переопределяются конфигом агента)
DEFAULT_AGENT_PORT = 8383
DEFAULT_WORK_DIR = r"C:\MagazAgent"
DEFAULT_BASE_1C_PATH = "C:/Program Files (x86)/1cv8"
DEFAULT_FILE_NAME = "update"
DEFAULT_LOG_DIR = Path(r"C:\ProgramData\MagazAgent\logs")

#: Д-1: значения по умолчанию расписания опроса оборудования (§4.9, §7.6).
#: Обычно их присылает сервер вместе со списком (Д-9), здесь — «до первого push-а».
DEFAULT_DEVICE_CHECK_INTERVAL_SEC = 60
DEFAULT_DEVICE_PING_TIMEOUT_MS = 1000

#: Имя файла конфига и переменные окружения для нестандартных развёртываний
AGENT_CONFIG_FILE_NAME = "agent-config.json"
#: Файл состояния текущей 1С-операции (§7.3): лежит рядом с конфигом
AGENT_STATE_FILE_NAME = "agent-state.json"
#: Копия списка оборудования подсети (§7.6): опрос идёт и без сервера
AGENT_DEVICES_FILE_NAME = "devices.json"
#: Очередь уведомлений и подтверждений (§7.4, ФН3): переживает перезапуск службы
AGENT_NOTIFICATIONS_FILE_NAME = "notifications.json"
CONFIG_PATH_ENV = "MAGAZ_AGENT_CONFIG"
LOG_DIR_ENV = "MAGAZ_AGENT_LOG_DIR"


class OneCProfile(BaseModel):
    """Профиль 1С на агенте (§7.2): логин и зашифрованный пароль (Fernet + DPAPI).

    Заполняется push-ом `POST /credentials` (П-1, этап Э4). В Э2 достаточно
    того, что версия профиля доступна в `/health`.
    """

    model_config = ConfigDict(extra="ignore")

    user_name: str = ""
    password_enc: str | None = None
    credentials_version: int = 1


class AgentConfig(BaseModel):
    """`agent-config.json` (§7.2) и поля, добавленные при реализации Д-2/Д-4."""

    model_config = ConfigDict(extra="ignore")

    store_id: int = 0
    store_name: str = ""
    server_url: str = ""
    agent_token: str = ""
    port: int = DEFAULT_AGENT_PORT
    base_1c_path: str = DEFAULT_BASE_1C_PATH
    default_file_name: str = DEFAULT_FILE_NAME
    one_c_profile: OneCProfile = Field(default_factory=OneCProfile)
    notifications_poll_sec: int = 20
    work_dir: str | None = None

    #: Д-2/Д-4: версия списка оборудования подсети, которой живёт агент (§7.6)
    devices_version: int = 1
    #: Д-1: расписание опроса оборудования подсети (§4.9). Актуальные значения
    #: приходят push-ом вместе со списком (Д-9); здесь — «до первого push-а»
    #: и для локальной отладки `--console` (§7.5)
    device_check_interval_sec: int = DEFAULT_DEVICE_CHECK_INTERVAL_SEC
    device_ping_timeout_ms: int = DEFAULT_DEVICE_PING_TIMEOUT_MS
    #: Д-4: адреса сервера, с которых принимаются запросы (вторая линия §5.1).
    #: Пусто — проверка источника выключена (так работают локальные прогоны).
    allowed_server_ips: list[str] = Field(default_factory=list)

    @property
    def credentials_version(self) -> int:
        """Версия учётных данных 1С, которую агент докладывает в `/health` (П-1)."""
        return int(self.one_c_profile.credentials_version)

    @property
    def work_path(self) -> Path:
        """Рабочая папка файлов обновления: из конфига либо от места запуска (§7.1)."""
        return Path(self.work_dir) if self.work_dir else get_application_root()

    @property
    def log_dir(self) -> Path:
        """Каталог логов службы (§7.1): `C:\\ProgramData\\MagazAgent\\logs`."""
        override = os.environ.get(LOG_DIR_ENV)
        return Path(override) if override else DEFAULT_LOG_DIR

    @property
    def state_path(self) -> Path:
        """`agent-state.json` рядом с рабочей папкой агента (§7.3)."""
        return self.work_path / AGENT_STATE_FILE_NAME

    @property
    def devices_path(self) -> Path:
        """Копия списка оборудования подсети: `C:\\MagazAgent\\devices.json` (§7.6)."""
        return self.work_path / AGENT_DEVICES_FILE_NAME

    @property
    def notifications_path(self) -> Path:
        """Очередь уведомлений: `C:\\MagazAgent\\notifications.json` (§7.4).

        Файл переживает перезапуск службы и недоступность сервера: `ack` и
        «отложено» не теряются на кассе (П-2).
        """
        return self.work_path / AGENT_NOTIFICATIONS_FILE_NAME


def get_application_root() -> Path:
    """Корень приложения: папка exe (frozen) либо корень репозитория (§7.1, I-14)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    """Путь `agent-config.json`: `MAGAZ_AGENT_CONFIG` либо рабочая папка агента."""
    override = os.environ.get(CONFIG_PATH_ENV)
    return Path(override) if override else get_application_root() / AGENT_CONFIG_FILE_NAME


def load_config(path: str | Path | None = None) -> AgentConfig:
    """Прочитать конфиг агента; отсутствие файла — ошибка с понятным текстом."""
    resolved = Path(path) if path else default_config_path()
    if not resolved.is_file():
        raise FileNotFoundError(
            f"Не найден конфиг агента: {resolved}. Возьмите шаблон в карточке магазина (§11.2)."
        )
    with resolved.open("r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    logger.debug("Конфиг агента прочитан: %s", resolved)
    return AgentConfig.model_validate(data)


def save_one_c_profile(path: str | Path, profile: "OneCProfile") -> Path:
    """Сохранить профиль 1С в `agent-config.json`, не трогая остальные поля (§7.2, П-1).

    Запись атомарная (временный файл + `os.replace`): если кассу выключат в
    момент push-а `POST /credentials`, конфиг останется читаемым — либо старый,
    либо новый. Значение пароля здесь уже зашифровано (Fernet + DPAPI).
    """
    target = Path(path)
    data: dict = {}
    if target.is_file():
        with target.open("r", encoding="utf-8-sig") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            data = loaded
    data["one_c_profile"] = {
        "user_name": profile.user_name,
        "password_enc": profile.password_enc,
        "credentials_version": int(profile.credentials_version),
    }

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, target)
    logger.info("Профиль 1С сохранён в %s (версия %s)", target, profile.credentials_version)
    return target


def describe_os() -> str:
    """Строка об ОС кассы для `/health` (§5.1.2, пример: «Windows Server 2019»)."""
    parts = [platform.system(), platform.release()]
    version = platform.version()
    if version:
        parts.append(f"({version})")
    return " ".join(part for part in parts if part).strip()
