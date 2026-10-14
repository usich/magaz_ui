"""Pydantic-схемы обмена между сервером и агентом (§5.1, §5.2.2).

Один источник истины: сервер формирует те же тела, что валидирует агент,
поэтому модели лежат в `shared`, а `client/agent/models.py` их реэкспортирует.

Перенос v2 (поведение 1:1, менять нельзя): `Base1CParams`, `MergeParams`,
`UpdateParams`, `MessageParams`, `DownloadParams`, `CheckFilesParams`.
Расширения: поля размеров/дат файлов, `HealthResponse`, `CredentialsPush`,
`NotifyRequest`, `DevicesConfigPush`, `DeviceStatusBatch`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import CheckType, DeviceStatus, DeviceType, NotificationSeverity, ReceiptStatus

__all__ = [
    "AckRequest",
    "AgentHealthReport",
    "Base1CParams",
    "CheckFilesParams",
    "CheckFilesResponse",
    "CommandResultReport",
    "CredentialsPush",
    "DeviceCheckRequest",
    "DeviceCheckResult",
    "DeviceCheckSpec",
    "DeviceSpec",
    "DeviceStatusBatch",
    "DevicesConfigPush",
    "DownloadParams",
    "HealthResponse",
    "JobStateResponse",
    "MergeParams",
    "MessageParams",
    "NotificationPendingItem",
    "NotifyRequest",
    "ReceiptReport",
    "UpdateParams",
]


def _validate_file_name(value: str) -> str:
    """Имя файла (без расширения) не должно содержать разделителей пути.

    Перенос v2 без изменений: иначе файл можно подменить/прочитать вне рабочей
    директории агента (инвариант I-2).
    """
    value = value.strip()
    if not value:
        raise ValueError("Имя файла не может быть пустым")
    if value in (".", "..") or "/" in value or "\\" in value:
        raise ValueError("Имя файла не должно содержать разделителей пути")
    return value


# --- Перенос v2 (§5.1.1) ---------------------------------------------------


class Base1CParams(BaseModel):
    """Базовые параметры подключения к 1С (перенос v2)."""

    server_name: str
    base_name: str
    version_1c: str
    user_name: str
    # П-1: пароль может отсутствовать — агент подставит свой профиль 1С
    password: str = ""

    @field_validator("server_name", "base_name", "version_1c", "user_name")
    @classmethod
    def not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Значение не может быть пустым")
        return value.strip()


class MergeParams(Base1CParams):
    """Параметры слияния конфигурации или расширения (перенос v2)."""

    file_name: str
    full_merge: bool = False
    extension_name: str | None = None

    @field_validator("file_name")
    @classmethod
    def check_file_name(cls, value: str) -> str:
        return _validate_file_name(value)


class UpdateParams(Base1CParams):
    """Параметры обновления конфигурации БД (перенос v2)."""

    is_fast_update: bool = False
    extension_name: str | None = None


class MessageParams(BaseModel):
    """Параметры отправки сообщения пользователям (перенос v2)."""

    text_message: str

    @field_validator("text_message")
    @classmethod
    def not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Текст сообщения не может быть пустым")
        return value


class DownloadParams(BaseModel):
    """Скачивание файла по ссылке (перенос v2; поле `url-file`)."""

    model_config = ConfigDict(populate_by_name=True)

    url_file: str = Field(..., alias="url-file")

    @field_validator("url_file")
    @classmethod
    def not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Ссылка на файл не может быть пустой")
        return value.strip()


class CheckFilesParams(BaseModel):
    """Проверка наличия файлов обновления (перенос v2)."""

    filename: str

    @field_validator("filename")
    @classmethod
    def check_filename(cls, value: str) -> str:
        return _validate_file_name(value)


class CheckFilesResponse(BaseModel):
    """Ответ `/check-files` (Q52: `*_size`/`*_mtime` — расширение, обратно совместимо)."""

    cf_exists: bool
    cfe_exists: bool
    xml_exists: bool
    cf_size: int | None = None
    cfe_size: int | None = None
    xml_size: int | None = None
    cf_mtime: str | None = None
    cfe_mtime: str | None = None
    xml_mtime: str | None = None


class JobStateResponse(BaseModel):
    """Ответ `/check-merge` и `/check-update-db` (перенос v2)."""

    status: str
    message: str
    logs: str | None = None


# --- Новые эндпоинты агента и самодоклады (§5.1.2, §5.2.2) ------------------


class BusyState(BaseModel):
    """Занятость агента 1С-операциями (`/health`, Q23)."""

    merge: bool = False
    update: bool = False


class AgentSession(BaseModel):
    """Активная сессия пользователя на кассе (Q50: 1–2 сессии)."""

    user: str
    sid: str


class HealthResponse(BaseModel):
    """`GET /health` агента (§5.1.2) — используется Health Monitor-ом сервера."""

    status: Literal["online"] = "online"
    store_id: int | None = None
    agent_version: str
    uptime_sec: int = 0
    work_dir: str | None = None
    base_1c_path: str | None = None
    os: str | None = None
    busy: BusyState = Field(default_factory=BusyState)
    sessions: list[AgentSession] = Field(default_factory=list)
    # П-1 и Д-1: по расхождению версий сервер понимает, что нужно повторно
    # отправить учётные данные 1С или список оборудования
    credentials_version: int = 0
    devices_version: int = 0
    interrupted_operation: str | None = None


class NotifyRequest(BaseModel):
    """`POST /notify` — приём уведомления от сервера (§5.1.2)."""

    id: int
    title: str
    body: str
    severity: NotificationSeverity = NotificationSeverity.INFO
    requires_ack: bool = False
    deadline_at: str | None = None
    repeat_every_min: int | None = None
    use_msg_exe: bool = True
    snooze_min: int = 15
    store_id: int | None = None

    @field_validator("title", "body")
    @classmethod
    def not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Значение не может быть пустым")
        return value


class NotificationPendingItem(BaseModel):
    """Уведомление, которое TrayAgent должен показать в сессии (§5.1.2)."""

    id: int
    title: str
    body: str
    severity: NotificationSeverity
    requires_ack: bool
    repeat_every_min: int | None = None
    snooze_until: str | None = None


class AckRequest(BaseModel):
    """`POST /notifications/{id}/ack` — «Ознакомлен» из сессии пользователя (§5.1.2)."""

    session_id: str
    session_user: str
    comment: str | None = None

    @field_validator("session_id", "session_user")
    @classmethod
    def not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Значение не может быть пустым")
        return value.strip()


class SessionRef(BaseModel):
    """Сессия Windows, от имени которой действует TrayAgent (§5.1.2, П-2).

    Служба и сервер не знают учётной записи сотрудника (П-6: справочника нет):
    адресат — логин Windows, а «своё окно и свой `receipt`» получает каждая
    локальная сессия кассы (Q50: 1–2 сессии на ПК).
    """

    session_id: str
    session_user: str

    @field_validator("session_id", "session_user")
    @classmethod
    def not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Значение не может быть пустым")
        return value.strip()


class SnoozeRequest(SessionRef):
    """`POST /notifications/{id}/snooze` — «Отложить» из сессии (П-2, Д-10).

    Интервал возврата задаёт **сервер** (`notification_snooze_min`, §4.9) и
    присылает его вместе с уведомлением: на кассе настройку не правят.
    """

    snooze_min: int | None = None


# --- П-1: учётные данные 1С -------------------------------------------------


class CredentialsPush(BaseModel):
    """`POST /credentials` — push учётных данных 1С сервер → агент (П-1, §5.1.2).

    Значение пароля живёт только в этом теле запроса: в логи, `commands.params`
    и аудит оно не попадает (I-7).
    """

    user_name: str
    password: str = ""
    credentials_version: int = 1

    @field_validator("user_name")
    @classmethod
    def not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Значение не может быть пустым")
        return value.strip()


# --- Д-1: оборудование подсети магазина -------------------------------------


class DeviceSpec(BaseModel):
    """Описание устройства в push-е списка оборудования (`POST /devices/config`)."""

    name: str
    ip: str
    check_type: CheckType = CheckType.ICMP
    port: int | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def validate_port(self) -> "DeviceSpec":
        """Порт обязателен при `check_type=tcp` (проверка после разбора полей)."""
        if self.check_type is CheckType.TCP and self.port is None:
            raise ValueError("Для check_type=tcp обязателен port")
        return self

    @field_validator("name", "ip")
    @classmethod
    def not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Значение не может быть пустым")
        return value.strip()


class DeviceCheckSpec(BaseModel):
    """Устройство для внеочередной проверки (`POST /devices/check-now`, §5.1.2).

    Имя здесь не нужно: сервер проверяет адреса, которые уже заведены в
    `store_devices`, а результат сопоставляется по IP (Д-1).
    """

    ip: str
    check_type: CheckType = CheckType.ICMP
    port: int | None = None

    @model_validator(mode="after")
    def validate_port(self) -> "DeviceCheckSpec":
        """Порт обязателен при `check_type=tcp` (проверка после разбора полей)."""
        if self.check_type is CheckType.TCP and self.port is None:
            raise ValueError("Для check_type=tcp обязателен port")
        return self

    @field_validator("ip")
    @classmethod
    def not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Значение не может быть пустым")
        return value.strip()


class DevicesConfigPush(BaseModel):
    """`POST /devices/config` — push списка оборудования сервер → агент (Д-1, §5.1.2).

    **Д-9:** вместе со списком приходит расписание опроса — интервал и таймаут
    проверок берутся из настроек сервера (§4.9), поэтому меняются в UI, а не
    правкой `agent-config.json` на каждой кассе.
    """

    devices_version: int
    devices: list[DeviceSpec] = Field(default_factory=list)
    #: Д-9: интервал опроса и таймаут одной проверки; `None` — оставить как было
    check_interval_sec: int | None = None
    ping_timeout_ms: int | None = None


class DeviceCheckRequest(BaseModel):
    """`POST /devices/check-now` — внеочередная проверка указанных IP (§5.1.2)."""

    devices: list[DeviceCheckSpec] = Field(default_factory=list)


class DeviceCheckResult(BaseModel):
    """Результат проверки одного устройства (ответ `/devices/check-now`, батч на сервер)."""

    ip: str
    status: DeviceStatus
    latency_ms: int | None = None
    error: str | None = None


class DeviceStatusBatch(BaseModel):
    """`POST /api/v1/agent/devices/status` — батч результатов опроса (Д-1, §5.2.2)."""

    store_id: int
    devices: list[DeviceCheckResult] = Field(default_factory=list)
    # Если касса не смогла опросить (служба остановлена) — сервер оставит `unknown`,
    # а не `offline` (§4.11)
    agent_ok: bool = True


# --- Самодоклады агента серверу (§5.2.2) -----------------------------------


class AgentHealthReport(BaseModel):
    """`POST /api/v1/agent/health` — агент сам доложил, что жив (Q46)."""

    store_id: int
    agent_version: str
    os: str | None = None
    work_dir: str | None = None
    base_1c_path: str | None = None
    credentials_version: int = 0
    devices_version: int = 0
    busy: BusyState = Field(default_factory=BusyState)
    sessions: list[AgentSession] = Field(default_factory=list)


class ReceiptItem(BaseModel):
    """Одно подтверждение по сессии в пакете от агента (§4.8, П-2).

    `status` — то, что видит пользователь: `shown` (тост `info` показан),
    `acknowledged` («Ознакомлен»), `snoozed` («Отложить»), `expired` (дедлайн).
    """

    notification_id: int
    session_id: str
    session_user: str
    status: ReceiptStatus
    comment: str | None = None
    snooze_until: str | None = None
    shown_at: str | None = None


class ReceiptReport(ReceiptItem):
    """`POST /api/v1/agent/receipts` — одиночное подтверждение напрямую от агента (§5.2.2)."""

    store_id: int


class ReceiptBatch(BaseModel):
    """`POST /api/v1/agent/receipts` — пакет подтверждений и состав сессий (§5.2.2).

    Пакет нужен для очереди (§7.4): пока касса не видит сервер, `ack` и
    «отложено» копятся в `notifications.json` и досылаются целиком при первой
    связи. Список `sessions` — это то, что TrayAgent реально видит на кассе: по
    нему сервер создаёт `pending`-строки, поэтому «X из Y» честно показывает
    «продавец2 — ожидание» (§4.8).
    """

    store_id: int
    receipts: list[ReceiptItem] = Field(default_factory=list)
    sessions: list[AgentSession] = Field(default_factory=list)


class CommandResultReport(BaseModel):
    """`POST /api/v1/agent/commands/{id}/result` — досылка результата задачи (§5.2.2)."""

    store_id: int
    status: str
    message: str | None = None
    logs: str | None = None
    payload: dict[str, Any] | None = None


