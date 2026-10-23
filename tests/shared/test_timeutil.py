"""Тесты `shared.timeutil` и `shared.enums`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shared.enums import (
    CheckType,
    CommandKind,
    CommandStatus,
    DeviceStatus,
    DeviceType,
    NotificationSeverity,
    ReceiptStatus,
    TargetType,
)
from shared.timeutil import to_iso_z, utcnow


class TestUtcNow:
    """Единый формат времени: UTC без tzinfo (иначе SQLite теряет зону)."""

    def test_is_naive_and_close_to_real_utc(self) -> None:
        now = utcnow()
        assert now.tzinfo is None
        assert abs(now - datetime.now(timezone.utc).replace(tzinfo=None)) < timedelta(seconds=5)

    def test_comparable_with_stored_values(self) -> None:
        old = utcnow() - timedelta(minutes=5)
        assert utcnow() > old


class TestToIsoZ:
    """Отдача меток в API: ISO-8601 с суффиксом `Z` и без микросекунд."""

    def test_none_stays_none(self) -> None:
        assert to_iso_z(None) is None

    def test_format(self) -> None:
        value = datetime(2026, 10, 13, 12, 30, 45, 123456)
        assert to_iso_z(value) == "2026-10-13T12:30:45Z"


class TestEnumValues:
    """Строковые значения Enum-ов — контракт с агентом и содержимое колонок TEXT."""

    def test_command_kinds_match_spec(self) -> None:
        assert {kind.value for kind in CommandKind} == {
            "check_files",
            "download_file",
            "run_merge",
            "run_update_db",
            "send_message",
            "notify",
            #: 1.23: отзыв уже доставленного уведомления с кассы
            "notify_cancel",
            #: Э10: удаление файлов обновления на кассе
            "delete_file",
            #: Д-24: обновление локальной базы
            "run_update_db_local",
            #: Д-26: проверка файлов и слияние локальной базы
            "check_files_local",
            "run_merge_local",
        }

    def test_command_statuses_match_spec(self) -> None:
        assert CommandStatus.SUCCESS.value == "success"
        assert CommandStatus.QUEUED.value == "queued"
        assert CommandStatus.TIMEOUT.value == "timeout"

    def test_severities_and_receipt_statuses(self) -> None:
        assert {item.value for item in NotificationSeverity} == {"info", "important", "critical"}
        assert {item.value for item in ReceiptStatus} == {
            "pending",
            "shown",
            "acknowledged",
            "snoozed",
            "expired",
        }

    def test_device_enums(self) -> None:
        assert {item.value for item in DeviceType} == {"camera", "switch", "printer", "pc", "other"}
        assert {item.value for item in CheckType} == {"icmp", "tcp"}
        assert {item.value for item in DeviceStatus} == {"online", "offline", "unknown"}

    def test_target_types(self) -> None:
        assert {item.value for item in TargetType} == {"all", "stores", "cluster"}

    def test_enum_serializes_as_string(self) -> None:
        assert f"{CommandStatus.SUCCESS}" == "success"
        assert CommandStatus.SUCCESS == "success"
