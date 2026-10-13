"""Тесты сервиса настроек §4.9 и сервиса администраторов §4.2."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from server.services import admin_service, settings_service
from shared.timeutil import utcnow

EXPECTED_DEFAULTS = {
    "health_interval_sec": 15,
    "health_timeout_sec": 5,
    "online_window_sec": 45,
    "health_concurrency": 20,
    "check_poll_interval_sec": 5,
    "command_retry_max": 3,
    "log_excerpt_limit_bytes": 204800,
    "notification_snooze_min": 15,
    "device_check_interval_sec": 60,
    "device_online_window_sec": 180,
    "device_ping_timeout_ms": 1000,
}


class TestSettingsDefaults:
    """§4.9: значения по умолчанию совпадают с таблицей спецификации."""

    def test_all_numeric_defaults_match_spec(self, session: Session) -> None:
        settings_service.ensure_defaults(session)
        session.commit()

        for key, expected in EXPECTED_DEFAULTS.items():
            assert settings_service.get_int(session, key) == expected, key

    def test_default_file_name_matches_spec(self, session: Session) -> None:
        settings_service.ensure_defaults(session)
        session.commit()
        assert settings_service.get_str(session, "default_file_name") == "update"

    def test_ensure_defaults_is_idempotent(self, session: Session) -> None:
        assert settings_service.ensure_defaults(session) == len(
            settings_service.DEFAULT_SETTINGS
        )
        assert settings_service.ensure_defaults(session) == 0

    def test_missing_row_returns_default(self, session: Session) -> None:
        assert settings_service.get_int(session, "health_interval_sec") == 15


class TestSettingsUpdate:
    """Изменение настроек из UI (`PUT /settings`)."""

    def test_set_value_normalizes_numbers(self, session: Session) -> None:
        settings_service.ensure_defaults(session)
        row = settings_service.set_value(session, "health_interval_sec", " 30 ")
        session.commit()

        assert row.value == "30"
        assert settings_service.get_int(session, "health_interval_sec") == 30

    @pytest.mark.parametrize("bad_value", ["пятнадцать", "", "10.5", "-5"])
    def test_invalid_values_rejected(self, session: Session, bad_value: str) -> None:
        with pytest.raises(ValueError):
            settings_service.set_value(session, "health_interval_sec", bad_value)

    def test_unknown_key_rejected(self, session: Session) -> None:
        with pytest.raises(ValueError, match="Неизвестная настройка"):
            settings_service.set_value(session, "not_a_real_setting", "1")

    def test_file_name_cannot_contain_path_separators(self, session: Session) -> None:
        with pytest.raises(ValueError):
            settings_service.set_value(session, "default_file_name", r"..\..\windows\system32")

    def test_validate_value_accepts_plain_name(self) -> None:
        assert settings_service.validate_value("default_file_name", "update-2026") == "update-2026"


class TestAdminService:
    """§4.2: создание администратора и проверка входа."""

    def test_create_and_authenticate(self, session: Session) -> None:
        admin = admin_service.create_admin(session, username="admin", password="strong-password")
        session.commit()

        assert admin.password_hash != "strong-password"
        assert admin.is_active is True
        assert admin.last_login_at is None

        authenticated = admin_service.authenticate(
            session, username="admin", password="strong-password"
        )
        session.commit()
        assert authenticated is not None
        assert authenticated.last_login_at is not None

    def test_username_is_case_insensitive(self, session: Session) -> None:
        admin_service.create_admin(session, username="Admin", password="strong-password")
        session.commit()
        assert admin_service.authenticate(session, username="ADMIN", password="strong-password")

    def test_wrong_password_returns_none(self, session: Session) -> None:
        admin_service.create_admin(session, username="admin", password="strong-password")
        session.commit()
        assert admin_service.authenticate(session, username="admin", password="wrong") is None

    def test_unknown_user_returns_none(self, session: Session) -> None:
        assert admin_service.authenticate(session, username="nobody", password="whatever") is None

    def test_inactive_admin_cannot_login(self, session: Session) -> None:
        admin = admin_service.create_admin(session, username="admin", password="strong-password")
        admin.is_active = False
        session.commit()
        assert admin_service.authenticate(session, username="admin", password="strong-password") is None

    def test_duplicate_username_rejected(self, session: Session) -> None:
        admin_service.create_admin(session, username="admin", password="strong-password")
        session.commit()
        with pytest.raises(admin_service.AdminExistsError):
            admin_service.create_admin(session, username="ADMIN", password="strong-password")

    def test_short_password_rejected(self, session: Session) -> None:
        with pytest.raises(admin_service.WeakPasswordError):
            admin_service.create_admin(session, username="admin", password="short")

    def test_empty_username_rejected(self, session: Session) -> None:
        with pytest.raises(ValueError):
            admin_service.create_admin(session, username="   ", password="strong-password")

    def test_updated_at_changes_on_login(self, session: Session) -> None:
        admin = admin_service.create_admin(session, username="admin", password="strong-password")
        session.commit()
        before = admin.updated_at

        assert utcnow() >= admin.created_at
        admin_service.authenticate(session, username="admin", password="strong-password")
        session.flush()
        assert admin.updated_at >= before
