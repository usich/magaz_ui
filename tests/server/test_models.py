"""Тесты моделей §4: значения по умолчанию, уникальность, каскады, статусы."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.db.models import (
    Command,
    FileCheck,
    JobLog,
    Notification,
    NotificationReceipt,
    NotificationTarget,
    Setting,
    Store,
    StoreDevice,
)
from shared.timeutil import utcnow

STORE_KWARGS = dict(
    name="Магазин №1",
    ip_address="10.10.1.10",
    cluster_name="srv-1c",
    database_name="trade",
    version_1c="8.3.24.1548",
    user_name_1c="Администратор",
    agent_token="token-1",
)


def make_store(session: Session, **overrides) -> Store:  # noqa: ANN003
    """Магазин с реквизитами по умолчанию и точечными переопределениями."""
    store = Store(**{**STORE_KWARGS, **overrides})
    session.add(store)
    session.flush()
    return store


def make_notification(session: Session, **overrides) -> Notification:  # noqa: ANN003
    """Уведомление с разумными умолчаниями (адресация уточняется в тестах)."""
    data = {
        "title": "Обновление 1С",
        "body": "База будет обновлена в 20:00",
        "severity": "important",
        "target_type": "all",
    }
    data.update(overrides)
    notification = Notification(**data)
    session.add(notification)
    session.flush()
    return notification


class TestStoreDefaults:
    """§4.1: служебные поля заполняются без участия UI."""

    def test_service_defaults(self, session: Session) -> None:
        store = make_store(session)
        assert store.agent_port == 8383
        assert store.credentials_version == 1
        assert store.devices_version == 1
        assert store.is_active is True
        assert store.password_set is False
        assert store.created_at is not None and store.updated_at is not None

    def test_password_set_flag_without_exposing_value(self, session: Session) -> None:
        store = make_store(session, password_enc="gAAAAA-encrypted")
        assert store.password_set is True
        assert store.password_enc == "gAAAAA-encrypted"

    def test_unique_name_and_ip(self, session: Session) -> None:
        make_store(session)
        session.commit()  # первый магазин фиксируем: иначе rollback ниже снесёт и его

        with pytest.raises(IntegrityError):
            make_store(session, name="Магазин №1", ip_address="10.10.1.11", agent_token="token-2")
        session.rollback()

        with pytest.raises(IntegrityError):
            make_store(session, name="Магазин №2", ip_address="10.10.1.10", agent_token="token-3")
        session.rollback()

    def test_unique_agent_token(self, session: Session) -> None:
        make_store(session)
        with pytest.raises(IntegrityError):
            make_store(session, name="Магазин №3", ip_address="10.10.1.12", agent_token="token-1")
        session.rollback()


class TestStoreOnline:
    """§3.3: онлайн определяется по `last_seen_at` и окну мониторинга."""

    def test_online_within_window(self, session: Session) -> None:
        store = make_store(session, last_seen_at=utcnow() - timedelta(seconds=10))
        assert store.is_online(window_sec=45) is True

    def test_offline_outside_window(self, session: Session) -> None:
        store = make_store(session, last_seen_at=utcnow() - timedelta(seconds=120))
        assert store.is_online(window_sec=45) is False

    def test_never_seen_is_offline(self, session: Session) -> None:
        assert make_store(session).is_online(window_sec=45) is False


class TestStoreDevice:
    """§4.11: оборудование подсети магазина (Д-1)."""

    def test_defaults_and_unique_pair(self, session: Session) -> None:
        store = make_store(session)
        device = StoreDevice(store_id=store.id, name="камера вход", ip_address="10.10.1.21")
        session.add(device)
        session.flush()

        assert device.check_type == "icmp"
        assert device.is_active is True
        assert device.last_status is None
        assert device.check_port is None

        session.add(StoreDevice(store_id=store.id, name="камера склад", ip_address="10.10.1.21"))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_same_ip_in_other_store_allowed(self, session: Session) -> None:
        first = make_store(session)
        second = make_store(
            session, name="Магазин №2", ip_address="10.10.2.10", agent_token="token-2"
        )
        session.add(StoreDevice(store_id=first.id, name="камера", ip_address="192.168.0.21"))
        session.add(StoreDevice(store_id=second.id, name="камера", ip_address="192.168.0.21"))
        session.flush()

    def test_online_window_respects_status_and_time(self, session: Session) -> None:
        store = make_store(session)
        device = StoreDevice(
            store_id=store.id,
            name="свитч",
            ip_address="10.10.1.30",
            last_status="online",
            last_seen_at=utcnow() - timedelta(seconds=30),
        )
        session.add(device)
        session.flush()

        assert device.is_online(window_sec=180) is True
        device.last_status = "offline"
        assert device.is_online(window_sec=180) is False

    def test_cascade_delete_with_store(self, session: Session) -> None:
        store = make_store(session)
        session.add(StoreDevice(store_id=store.id, name="камера", ip_address="10.10.1.21"))
        session.commit()

        session.delete(store)
        session.commit()

        assert session.query(StoreDevice).count() == 0, "ON DELETE CASCADE должен работать (PRAGMA)"


class TestCommandsAndLogs:
    """§4.3–4.4: история операций и журнал задачи."""

    def test_command_with_logs_and_cascade(self, session: Session) -> None:
        store = make_store(session)
        command = Command(
            store_id=store.id,
            kind="run_merge",
            params={"file_name": "update", "full_merge": False},
            status="queued",
            created_by="admin",
        )
        session.add(command)
        session.flush()

        assert command.attempts == 0
        session.add(JobLog(command_id=command.id, level="info", message="задача поставлена"))
        session.commit()

        session.delete(store)
        session.commit()
        assert session.query(Command).count() == 0
        assert session.query(JobLog).count() == 0


class TestFileChecks:
    """§4.5: снимки проверки файлов (Q52)."""

    def test_snapshot_fields(self, session: Session) -> None:
        store = make_store(session)
        check = FileCheck(
            store_id=store.id,
            filename="update",
            cf_exists=True,
            cfe_exists=False,
            xml_exists=True,
            cf_size=1024,
            cf_mtime=utcnow(),
        )
        session.add(check)
        session.flush()

        assert check.checked_at is not None
        assert check.xml_size is None
        assert check.cf_exists is True


class TestNotifications:
    """§4.6–4.8: уведомления, цели и «ознакомлен» по сессиям (П-2)."""

    def test_target_and_receipt_progress(self, session: Session) -> None:
        store = make_store(session)
        notification = make_notification(
            session,
            requires_ack=True,
            target_type="stores",
            target_value=str(store.id),
            repeat_every_min=5,
            created_by="admin",
        )
        target = NotificationTarget(
            notification_id=notification.id, store_id=store.id, sessions_count=2
        )
        session.add(target)
        session.flush()

        ack = NotificationReceipt(
            target_id=target.id,
            session_user="user1",
            session_id="1",
            status="acknowledged",
            shown_at=utcnow(),
            acknowledged_at=utcnow(),
        )
        pending = NotificationReceipt(target_id=target.id, session_user="user2", session_id="2")
        session.add_all([ack, pending])
        session.flush()

        assert ack.is_acknowledged is True
        assert pending.is_acknowledged is False
        assert pending.status == "pending"
        assert pending.snooze_count == 0

    def test_snoozed_is_not_acknowledged(self, session: Session) -> None:
        store = make_store(session)
        target = NotificationTarget(
            notification_id=make_notification(session, severity="critical").id, store_id=store.id
        )
        session.add(target)
        session.flush()

        receipt = NotificationReceipt(
            target_id=target.id,
            session_user="user1",
            session_id="1",
            status="snoozed",
            snooze_count=2,
            snooze_until=utcnow() + timedelta(minutes=15),
        )
        session.add(receipt)
        session.flush()

        assert receipt.is_acknowledged is False

    def test_duplicate_session_receipt_rejected(self, session: Session) -> None:
        store = make_store(session)
        target = NotificationTarget(
            notification_id=make_notification(session, severity="info").id, store_id=store.id
        )
        session.add(target)
        session.flush()

        session.add(NotificationReceipt(target_id=target.id, session_user="user1", session_id="1"))
        session.flush()
        session.add(NotificationReceipt(target_id=target.id, session_user="user1", session_id="1"))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


class TestSettingModel:
    """§4.9: настройки key/value."""

    def test_as_int(self, session: Session) -> None:
        session.add(Setting(key="health_interval_sec", value="15"))
        session.flush()
        assert session.get(Setting, "health_interval_sec").as_int() == 15

