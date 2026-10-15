"""Enum-ы статусов, типов и кодов ответов (единый словарь сервера и агента).

Значения — строки, потому что они уходят в JSON-API, в БД (колонки TEXT) и в
`commands.params`. Менять строковые значения нельзя: это контракт с агентом.
"""

from enum import Enum

__all__ = [
    "ActionKind",
    "CheckType",
    "CommandKind",
    "CommandStatus",
    "DeviceStatus",
    "DeviceType",
    "JobLogLevel",
    "NotificationSeverity",
    "NotificationStatus",
    "ReceiptStatus",
    "TargetType",
]


class StrEnum(str, Enum):
    """Строковый Enum (совместим с JSON и SQLAlchemy TEXT-колонками)."""

    def __str__(self) -> str:  # pragma: no cover - удобство логирования
        return str(self.value)


class CommandKind(StrEnum):
    """Тип задачи в таблице `commands` (§4.3)."""

    CHECK_FILES = "check_files"
    DOWNLOAD_FILE = "download_file"
    RUN_MERGE = "run_merge"
    RUN_UPDATE_DB = "run_update_db"
    SEND_MESSAGE = "send_message"
    NOTIFY = "notify"
    #: Э10: удаление файлов обновления в рабочей папке кассы
    DELETE_FILE = "delete_file"


class CommandStatus(StrEnum):
    """Жизненный цикл задачи (§4.3)."""

    QUEUED = "queued"
    SENT = "sent"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"
    CANCELED = "canceled"


class ActionKind(StrEnum):
    """Действие админки над магазином (§5.2.2, /actions и /bulk/actions)."""

    CHECK_FILES = "check_files"
    DOWNLOAD_FILE = "download_file"
    RUN_MERGE = "run_merge"
    RUN_UPDATE_DB = "run_update_db"
    SEND_MESSAGE = "send_message"
    NOTIFY = "notify"
    #: Э10: удалить файлы обновления на кассе (`.cf/.cfe/.xml` в рабочей папке)
    DELETE_FILE = "delete_file"


class JobLogLevel(StrEnum):
    """Уровень строки журнала задачи (§4.4)."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class NotificationSeverity(StrEnum):
    """Уровни важности уведомлений (Q31)."""

    INFO = "info"
    IMPORTANT = "important"
    CRITICAL = "critical"


class NotificationStatus(StrEnum):
    """Статус рассылки в UI (§11.5): работа/завершено/отменено."""

    ACTIVE = "active"
    DONE = "done"
    CANCELED = "canceled"


class TargetType(StrEnum):
    """Адресация уведомления (§4.6)."""

    ALL = "all"
    STORES = "stores"
    CLUSTER = "cluster"


class ReceiptStatus(StrEnum):
    """Статус ознакомления по сессии пользователя (§4.8, П-2)."""

    PENDING = "pending"
    SHOWN = "shown"
    ACKNOWLEDGED = "acknowledged"
    SNOOZED = "snoozed"
    EXPIRED = "expired"


class DeviceType(StrEnum):
    """Тип оборудования подсети магазина (Д-1, §4.11)."""

    CAMERA = "camera"
    SWITCH = "switch"
    PRINTER = "printer"
    PC = "pc"
    OTHER = "other"


class CheckType(StrEnum):
    """Способ проверки устройства (Д-1, §4.11)."""

    ICMP = "icmp"
    TCP = "tcp"


class DeviceStatus(StrEnum):
    """Статус устройства подсети (Д-1, §4.11)."""

    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"
