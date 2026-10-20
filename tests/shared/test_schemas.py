"""Тесты схем обмена `shared.schemas` (§5.1): перенос v2 и расширения."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.enums import CheckType, DeviceStatus, NotificationSeverity, ReceiptStatus
from shared.schemas import (
    AckRequest,
    AgentHealthReport,
    CheckFilesParams,
    CredentialsPush,
    DeviceCheckResult,
    DeviceSpec,
    DevicesConfigPush,
    DeviceStatusBatch,
    DownloadParams,
    HealthResponse,
    MergeParams,
    MessageParams,
    NotificationPendingItem,
    NotifyRequest,
    ReceiptReport,
    UpdateParams,
)


def merge_payload(**overrides) -> dict:  # noqa: ANN003
    payload = {
        "server_name": "srv-1c",
        "base_name": "trade",
        "version_1c": "8.3.24.1548",
        "user_name": "Администратор",
        "password": "secret",
        "file_name": "update",
    }
    payload.update(overrides)
    return payload


class TestV2Params:
    """§5.1.1: поведение переносится 1:1 — поля и валидация те же."""

    def test_merge_defaults(self) -> None:
        params = MergeParams(**merge_payload())
        assert params.full_merge is False
        assert params.extension_name is None
        assert params.settings_file_name is None

    def test_settings_file_name_optional_and_validated(self) -> None:
        # Пусто/пропущено → None (по умолчанию full_merge.xml на агенте).
        assert MergeParams(**merge_payload(settings_file_name="")).settings_file_name is None
        assert (
            MergeParams(**merge_payload(settings_file_name="частичное")).settings_file_name
            == "частичное"
        )
        # Разделители пути — ошибка (как и для file_name).
        with pytest.raises(ValidationError):
            MergeParams(**merge_payload(settings_file_name="../evil"))

    def test_update_defaults(self) -> None:
        params = UpdateParams(**merge_payload())
        assert params.is_fast_update is False
        assert params.extension_name is None

    def test_password_may_be_omitted(self) -> None:
        payload = merge_payload()
        payload.pop("password")
        assert MergeParams(**payload).password == "", "агент подставит свой профиль (П-1)"

    @pytest.mark.parametrize("field", ["server_name", "base_name", "version_1c", "user_name"])
    def test_empty_fields_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError):
            MergeParams(**merge_payload(**{field: "   "}))

    @pytest.mark.parametrize("bad_name", ["", "..", "sub/update", "sub\\update"])
    def test_file_name_separators_rejected(self, bad_name: str) -> None:
        with pytest.raises(ValidationError):
            CheckFilesParams(filename=bad_name)
        with pytest.raises(ValidationError):
            MergeParams(**merge_payload(file_name=bad_name))

    def test_values_are_stripped(self) -> None:
        params = MergeParams(**merge_payload(server_name=" srv-1c ", base_name=" trade "))
        assert params.server_name == "srv-1c"
        assert params.base_name == "trade"

    def test_message_text_required(self) -> None:
        assert MessageParams(text_message="Внимание").text_message == "Внимание"
        with pytest.raises(ValidationError):
            MessageParams(text_message="   ")

    def test_download_accepts_hyphenated_alias(self) -> None:
        assert DownloadParams(**{"url-file": "https://disk.yandex.ru/d/XXXX"}).url_file.endswith("XXXX")
        assert DownloadParams(url_file="https://example.com/x.zip").url_file == "https://example.com/x.zip"
        with pytest.raises(ValidationError):
            DownloadParams(**{"url-file": "  "})


class TestAgentHealthSchema:
    """§5.1.2: `/health` агента."""

    def test_defaults_for_optional_blocks(self) -> None:
        health = HealthResponse(agent_version="1.0.0")
        assert health.status == "online"
        assert health.busy.merge is False and health.busy.update is False
        assert health.sessions == []
        assert health.credentials_version == 0 and health.devices_version == 0

    def test_sessions_and_busy_parsed(self) -> None:
        health = HealthResponse(
            agent_version="1.0.0",
            store_id=12,
            busy={"merge": True, "update": False},
            sessions=[{"user": "user1", "sid": "1"}, {"user": "user2", "sid": "2"}],
            credentials_version=3,
            devices_version=4,
        )
        assert health.store_id == 12
        assert health.busy.merge is True
        assert [session.user for session in health.sessions] == ["user1", "user2"]
        assert health.credentials_version == 3 and health.devices_version == 4

    def test_agent_version_is_required(self) -> None:
        with pytest.raises(ValidationError):
            HealthResponse()
