"""Pydantic-схемы агента (§5.1): перенос v2 + расширения.

Единый источник истины по телам запросов и ответов — `shared/schemas.py`
(§6: «сервер формирует те же тела, что валидирует агент»), поэтому здесь они
реэкспортируются. Дополнительно описан ответ `GET /version`, который есть
только у агента (§5.1.2).
"""

from __future__ import annotations

from pydantic import BaseModel

from shared.schemas import (
    AckRequest,
    Base1CParams,
    CheckFilesParams,
    CheckFilesResponse,
    CredentialsPush,
    DeviceCheckRequest,
    DeviceCheckResult,
    DeviceSpec,
    DevicesConfigPush,
    DownloadParams,
    HealthResponse,
    JobStateResponse,
    MergeParams,
    MessageParams,
    NotificationPendingItem,
    NotifyCancelRequest,
    NotifyRequest,
    UpdateParams,
)

__all__ = [
    "AckRequest",
    "AgentVersionResponse",
    "Base1CParams",
    "CheckFilesParams",
    "CheckFilesResponse",
    "CredentialsPush",
    "DeviceCheckRequest",
    "DeviceCheckResult",
    "DeviceSpec",
    "DevicesConfigPush",
    "DownloadParams",
    "HealthResponse",
    "JobStateResponse",
    "MergeParams",
    "MessageParams",
    "NotificationPendingItem",
    "NotifyCancelRequest",
    "NotifyRequest",
    "UpdateParams",
]


class AgentVersionResponse(BaseModel):
    """`GET /version` агента (§5.1.2) — версия агента и рабочие пути."""

    store_id: int
    store_name: str = ""
    agent_version: str
    port: int
    work_dir: str
    base_1c_path: str
    default_file_name: str
    config_path: str | None = None
    uptime_sec: int = 0
    os: str
    credentials_version: int = 0
    devices_version: int = 0
