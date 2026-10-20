"""Тесты уведомлений (§4.6–4.8, §5.2.2, §11.5) — состав Э5 (ФН3).

Сеть не нужна: доставка проверяется на моке кассы `ScriptedAgent` (§13), а
подтверждения приходят тем же путём, каким их присылает агент —
`POST /api/v1/agent/receipts` (§7.4). Главные правила, за которые отвечают
проверки:

* рассылка не выполняется в HTTP-запросе: создаются задачи `notify`, доставку
  делает диспетчер (§11.4, I-8);
* «X из Y» считается по строкам `notification_receipts`, а «Отложить» (П-2)
  числитель **не** увеличивает;
* позднее `shown` из буфера агента не откатывает уже поставленное «Ознакомлен»;
* отмена рассылки закрывает ещё не отправленные задачи и останавливает показы.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from server.config import Settings
from server.core.dispatcher import CommandDispatcher
from server.db.models import (
    AuditLog,
    Command,
    NotificationReceipt,
    NotificationTarget,
    Store,
)
from server.db.session import DatabaseManager
from server.services import command_service, notification_service, settings_service
from shared.enums import CommandKind, CommandStatus, ReceiptStatus
from shared.timeutil import to_iso_z, utcnow
from tests.server.agent_mock import ScriptedAgent
from tests.server.conftest import ADMIN_PASSWORD, ADMIN_USERNAME

NOTIFICATIONS_URL = "/api/v1/admin/notifications"
RECEIPTS_URL = "/api/v1/agent/receipts"
SYSTEM_INFO_URL = "/api/v1/admin/system/info"
LOGIN_URL = "/api/v1/admin/auth/login"


def login(client: TestClient) -> TestClient:
    """Войти в дашборд — фабрика `make_client` отдаёт клиент без сессии."""
    response = client.post(
        LOGIN_URL, json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return client


def create_notification(client: TestClient, **overrides: object) -> dict:
    """Создать уведомление через API — так же, как это делает админ (§11.5)."""
    payload: dict = {
        "title": "Смена закрывается в 22:00",
        "body": "Сохраните смену и отключите кассу",
        "severity": "important",
    }
    payload.update(overrides)
    response = client.post(NOTIFICATIONS_URL, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def send_receipts(
    client: TestClient,
    store_id: int,
    *,
    headers: dict[str, str],
    receipts: list[dict] | None = None,
    sessions: list[dict] | None = None,
) -> dict:
    """Прислать пакет подтверждений от кассы (как это делает агент, §7.4)."""
    response = client.post(
        RECEIPTS_URL,
        json={"store_id": store_id, "receipts": receipts or [], "sessions": sessions or []},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def receipt(
    notification_id: int, *, status: str, session_id: str = "1", **extra: object
) -> dict:
    """Строка подтверждения по сессии Windows (§4.8)."""
    payload: dict = {
        "notification_id": notification_id,
        "session_id": session_id,
        "session_user": f"user{session_id}",
        "status": status,
    }
    payload.update(extra)
    return payload


def notify_jobs(session: Session) -> list[Command]:
    """Все задачи рассылки уведомлений (§4.3)."""
    return list(
        session.scalars(
            select(Command).where(Command.kind == str(CommandKind.NOTIFY)).order_by(Command.id)
        ).all()
    )


def add_store(
    session: Session,
    *,
    name: str,
    ip_address: str,
    token: str,
    cluster: str = "srv-1c",
    is_active: bool = True,
) -> Store:
    """Завести магазин напрямую в БД — так адресация получает второго получателя."""
    store = Store(
        name=name,
        ip_address=ip_address,
        cluster_name=cluster,
        database_name="trade",
        version_1c="8.3.24.1548",
        user_name_1c="Администратор",
        agent_token=token,
        is_active=is_active,
    )
    session.add(store)
    session.commit()
    return store


class TestNotificationsAuth:
    """Точки уведомлений закрыты сессией администратора (§8.1)."""

    def test_endpoints_require_auth(self, client: TestClient) -> None:
        assert client.get(NOTIFICATIONS_URL).status_code == 401
        assert client.post(NOTIFICATIONS_URL, json={}).status_code == 401
        assert client.get(f"{NOTIFICATIONS_URL}/1").status_code == 401
        assert client.get(f"{NOTIFICATIONS_URL}/1/report").status_code == 401
        assert client.post(f"{NOTIFICATIONS_URL}/1/cancel").status_code == 401

    def test_agent_receipts_require_token(self, client: TestClient) -> None:
        assert client.post(RECEIPTS_URL, json={"store_id": 1}).status_code == 401


class TestCreateNotification:
    """Создание и адресация: «все», кластер, список магазинов (§4.6, §11.4)."""

    def test_creates_jobs_for_every_target(  # noqa: ANN001
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        add_store(session, name="Магазин №6", ip_address="10.10.5.11", token="token-6", cluster="srv-2")

        body = create_notification(auth_client, severity="critical")

        assert body["stores"] == 2
        assert body["jobs"] == 2, "рассылку выполняет диспетчер, а не HTTP-запрос (I-8)"
        notification = notification_service.get_notification(
            session, body["notification"]["id"]
        )
        assert [target.store_id for target in notification.targets] == [
            store.id,
            store.id + 1,
        ]
        assert notification.severity == "critical"
        assert notification.target_type == "all"

        jobs = notify_jobs(session)
        assert [job.params for job in jobs] == [
            {"notification_id": notification.id},
            {"notification_id": notification.id},
        ]
        assert all(job.status == str(CommandStatus.QUEUED) for job in jobs)
        actions = [row.action for row in session.scalars(select(AuditLog)).all()]
        assert "notification_create" in actions

    def test_stores_audience_requires_ids(self, auth_client: TestClient) -> None:
        response = auth_client.post(
            NOTIFICATIONS_URL,
            json={"title": "Заголовок", "body": "Текст", "target_type": "stores"},
        )

        assert response.status_code == 422
        assert "store_ids" in response.json()["detail"]

    def test_missing_body_is_422(self, auth_client: TestClient) -> None:
        response = auth_client.post(NOTIFICATIONS_URL, json={"title": "Только заголовок"})

        assert response.status_code == 422
        assert "body" in response.text, "тело уведомления обязательно (§4.6)"

    def test_cluster_audience(  # noqa: ANN001
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        add_store(
            session,
            name="Магазин №7",
            ip_address="10.10.5.12",
            token="token-7",
            cluster="other-cluster",
        )

        body = create_notification(
            auth_client, target_type="cluster", cluster=store.cluster_name
        )

        assert body["stores"] == 1
        notification = notification_service.get_notification(
            session, body["notification"]["id"]
        )
        assert notification.target_value == store.cluster_name

    def test_disabled_store_is_skipped_with_reason(  # noqa: ANN001
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        disabled = add_store(
            session,
            name="Магазин №8",
            ip_address="10.10.5.13",
            token="token-8",
            cluster=store.cluster_name,
            is_active=False,
        )

        body = create_notification(
            auth_client, target_type="stores", store_ids=[store.id, disabled.id]
        )

        assert body["stores"] == 1
        assert body["skipped_store_ids"] == [disabled.id], (
            "выключенный магазин не получает уведомление, но админ видит причину"
        )


def deliver(session: Session, store: Store, notification_id: int, *, sessions: int = 2) -> None:
    """Отметить доставку так, как её отмечает диспетчер после `POST /notify` (§4.7)."""
    notification = notification_service.get_notification(session, notification_id)
    notification_service.mark_delivered(session, store, notification, sessions=sessions)
    session.commit()


class TestAgentReceipts:
    """«X из Y»: состав сессий и подтверждения от кассы (§4.8, П-2)."""

    def test_sessions_create_pending_receipts(  # noqa: ANN001
        self,
        auth_client: TestClient,
        session: Session,
        store: Store,
        agent_headers: dict[str, str],
    ) -> None:
        notification_id = create_notification(auth_client)["notification"]["id"]
        deliver(session, store, notification_id)

        summary = send_receipts(
            auth_client,
            store.id,
            headers=agent_headers,
            sessions=[
                {"user": "user1", "sid": "1"},
                {"user": "user2", "sid": "2"},
            ],
        )

        assert summary["created"] == 2, "«продавец2 — ожидание» видно сразу (§4.8)"
        report = auth_client.get(f"{NOTIFICATIONS_URL}/{notification_id}").json()
        totals = report["totals"]
        assert (totals["receipts"], totals["pending"], totals["acknowledged"]) == (2, 2, 0)
        assert [item["status"] for item in report["stores"][0]["receipts"]] == [
            ReceiptStatus.PENDING,
            ReceiptStatus.PENDING,
        ]

    def test_acknowledged_counts_in_progress(  # noqa: ANN001
        self,
        auth_client: TestClient,
        session: Session,
        store: Store,
        agent_headers: dict[str, str],
    ) -> None:
        notification_id = create_notification(auth_client, requires_ack=True)["notification"][
            "id"
        ]
        deliver(session, store, notification_id)
        send_receipts(
            auth_client,
            store.id,
            headers=agent_headers,
            sessions=[{"user": "user1", "sid": "1"}, {"user": "user2", "sid": "2"}],
        )

        summary = send_receipts(
            auth_client,
            store.id,
            headers=agent_headers,
            receipts=[receipt(notification_id, status="acknowledged", comment="принял")],
        )

        assert summary["updated"] == 1
        report = auth_client.get(f"{NOTIFICATIONS_URL}/{notification_id}").json()
        assert report["totals"]["acknowledged"] == 1
        assert report["totals"]["percent"] == 50
        acknowledged = report["stores"][0]["receipts"][0]
        assert acknowledged["is_acknowledged"] is True
        assert acknowledged["ack_comment"] == "принял"
        assert acknowledged["acknowledged_at"] is not None

    def test_snoozed_is_not_acknowledged(  # noqa: ANN001
        self,
        auth_client: TestClient,
        session: Session,
        store: Store,
        agent_headers: dict[str, str],
    ) -> None:
        """П-2/FR-26: «Отложить» оставляет уведомление непрочитанным."""
        notification_id = create_notification(auth_client)["notification"]["id"]
        deliver(session, store, notification_id)
        send_receipts(
            auth_client,
            store.id,
            headers=agent_headers,
            sessions=[{"user": "user1", "sid": "1"}],
        )
        until = to_iso_z(utcnow() + timedelta(minutes=15))

        send_receipts(
            auth_client,
            store.id,
            headers=agent_headers,
            receipts=[
                receipt(notification_id, status="snoozed", snooze_until=until)
            ],
        )

        report = auth_client.get(f"{NOTIFICATIONS_URL}/{notification_id}").json()
        assert report["totals"]["acknowledged"] == 0, "отложенное не ознакомлено (П-2)"
        assert report["totals"]["snoozed"] == 1
        row = report["stores"][0]["receipts"][0]
        assert row["status"] == str(ReceiptStatus.SNOOZED)
        assert row["snooze_count"] == 1
        assert row["snooze_until"] == until
        assert row["is_acknowledged"] is False

        stored = session.scalars(select(NotificationReceipt)).all()
        assert [item.acknowledged_at for item in stored] == [None]

    def test_late_shown_does_not_downgrade_acknowledged(  # noqa: ANN001
        self,
        auth_client: TestClient,
        session: Session,
        store: Store,
        agent_headers: dict[str, str],
    ) -> None:
        """Позднее `shown` из буфера агента не откатывает «Ознакомлен» (§7.4)."""
        notification_id = create_notification(auth_client)["notification"]["id"]
        deliver(session, store, notification_id)
        send_receipts(
            auth_client,
            store.id,
            headers=agent_headers,
            receipts=[receipt(notification_id, status="acknowledged")],
        )

        summary = send_receipts(
            auth_client,
            store.id,
            headers=agent_headers,
            receipts=[receipt(notification_id, status="shown")],
        )

        assert summary["ignored"] == 1
        report = auth_client.get(f"{NOTIFICATIONS_URL}/{notification_id}").json()
        assert report["totals"]["acknowledged"] == 1
        assert report["stores"][0]["receipts"][0]["status"] == str(
            ReceiptStatus.ACKNOWLEDGED
        )

    def test_deadline_marks_expired(  # noqa: ANN001
        self,
        auth_client: TestClient,
        session: Session,
        store: Store,
        agent_headers: dict[str, str],
    ) -> None:
        """§4.6/§4.8: не ознакомились к дедлайну — строка закрывается `expired`."""
        notification_id = create_notification(
            auth_client, deadline_at=to_iso_z(utcnow() - timedelta(hours=1))
        )["notification"]["id"]
        deliver(session, store, notification_id)

        summary = send_receipts(
            auth_client,
            store.id,
            headers=agent_headers,
            sessions=[{"user": "user1", "sid": "1"}],
        )

        assert summary["expired"] == 1
        report = auth_client.get(f"{NOTIFICATIONS_URL}/{notification_id}").json()
        row = report["stores"][0]["receipts"][0]
        assert row["status"] == str(ReceiptStatus.EXPIRED)
        assert row["is_overdue"] is True
        assert report["totals"]["expired"] == 1

    def test_unknown_notification_is_ignored(  # noqa: ANN001
        self, auth_client: TestClient, store: Store, agent_headers: dict[str, str]
    ) -> None:
        """Касса не может «дописать» себе уведомление чужого магазина."""
        summary = send_receipts(
            auth_client,
            store.id,
            headers=agent_headers,
            receipts=[receipt(999, status="acknowledged")],
        )

        assert summary == {
            "ok": True,
            "store_id": store.id,
            "created": 0,
            "updated": 0,
            "ignored": 1,
            "expired": 0,
        }


class TestNotificationsList:
    """Список, счётчики и плитка главной (§11.1, §11.5)."""

    def test_list_shows_progress_and_counters(  # noqa: ANN001
        self,
        auth_client: TestClient,
        session: Session,
        store: Store,
        agent_headers: dict[str, str],
    ) -> None:
        notification_id = create_notification(auth_client)["notification"]["id"]
        deliver(session, store, notification_id)
        send_receipts(
            auth_client,
            store.id,
            headers=agent_headers,
            sessions=[{"user": "user1", "sid": "1"}, {"user": "user2", "sid": "2"}],
        )

        body = auth_client.get(NOTIFICATIONS_URL).json()

        assert body["total"] == 1
        assert body["counts"]["active"] == 1
        assert body["pending_receipts"] == 2, "плитка «Ждут ознакомления» (§11.1)"
        assert body["snooze_min"] == 15, "интервал возврата виден в UI (§4.9, П-2)"
        item = body["items"][0]
        assert item["store_names"] == [store.name]
        assert item["totals"]["receipts"] == 2
        assert item["state"] == "active"

        counters = auth_client.get(SYSTEM_INFO_URL).json()["counts"]
        assert counters["notifications_pending"] == 2
        assert counters["notifications_snooze_min"] == 15

    def test_filters_by_status_and_store(  # noqa: ANN001
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        other = add_store(session, name="Магазин №9", ip_address="10.10.5.14", token="token-9")

        first = create_notification(auth_client, target_type="stores", store_ids=[store.id])[
            "notification"
        ]["id"]
        create_notification(auth_client, target_type="stores", store_ids=[other.id])
        auth_client.post(f"{NOTIFICATIONS_URL}/{first}/cancel")

        by_store = auth_client.get(NOTIFICATIONS_URL, params={"store_id": other.id}).json()
        active = auth_client.get(NOTIFICATIONS_URL, params={"status": "active"}).json()
        canceled = auth_client.get(NOTIFICATIONS_URL, params={"status": "canceled"}).json()

        assert [item["id"] for item in by_store["items"]] != [first]
        assert by_store["total"] == 1
        assert canceled["total"] == 1 and canceled["items"][0]["id"] == first
        assert active["total"] == 1 and active["items"][0]["id"] != first
        assert auth_client.get(NOTIFICATIONS_URL, params={"status": "nonsense"}).status_code == 422


class TestReportAndCsv:
    """Отчёт «магазины → сессии» и выгрузка CSV (§11.5)."""

    def test_report_matches_facts_and_exports_csv(  # noqa: ANN001
        self,
        auth_client: TestClient,
        session: Session,
        store: Store,
        agent_headers: dict[str, str],
    ) -> None:
        notification_id = create_notification(auth_client, requires_ack=True)["notification"][
            "id"
        ]
        deliver(session, store, notification_id)
        send_receipts(
            auth_client,
            store.id,
            headers=agent_headers,
            sessions=[{"user": "user1", "sid": "1"}, {"user": "user2", "sid": "2"}],
        )
        send_receipts(
            auth_client,
            store.id,
            headers=agent_headers,
            receipts=[receipt(notification_id, status="acknowledged")],
        )

        report = auth_client.get(f"{NOTIFICATIONS_URL}/{notification_id}/report").json()

        assert report["notification"]["id"] == notification_id
        assert report["snooze_min"] == 15
        assert report["stores"][0]["store_name"] == store.name
        assert report["stores"][0]["delivered_at"] is not None
        assert report["stores"][0]["sessions_count"] == 2
        statuses = [item["status"] for item in report["stores"][0]["receipts"]]
        assert statuses == [str(ReceiptStatus.ACKNOWLEDGED), str(ReceiptStatus.PENDING)]

        exported = auth_client.get(
            f"{NOTIFICATIONS_URL}/{notification_id}/report", params={"format": "csv"}
        )
        text = exported.text

        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith("text/csv")
        assert f"notification-{notification_id}.csv" in exported.headers["content-disposition"]
        assert text.startswith("\ufeff"), "BOM: Excel открывает файл без «кракозябр»"
        assert "Магазин;Сессия (пользователь);Session ID;Статус" in text
        assert "ознакомлен" in text and "ожидание" in text
        assert store.name in text

    def test_unknown_notification_is_404(self, auth_client: TestClient) -> None:
        assert auth_client.get(f"{NOTIFICATIONS_URL}/999").status_code == 404
        assert auth_client.get(f"{NOTIFICATIONS_URL}/999/report").status_code == 404


class TestCancel:
    """Отмена рассылки: показы прекращаются, незавершённые задачи закрываются (§11.5)."""

    def test_cancel_closes_queued_jobs(  # noqa: ANN001
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        notification_id = create_notification(auth_client)["notification"]["id"]

        response = auth_client.post(f"{NOTIFICATIONS_URL}/{notification_id}/cancel")

        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "canceled"
        assert body["canceled_jobs"] == 1
        assert all(
            job.status == str(CommandStatus.CANCELED) for job in notify_jobs(session)
        ), "уведомление не придёт на кассу после отмены"
        assert auth_client.post(f"{NOTIFICATIONS_URL}/{notification_id}/cancel").status_code == 409
        actions = [row.action for row in session.scalars(select(AuditLog)).all()]
        assert "notification_cancel" in actions

    def test_cancel_revokes_delivered_store(  # noqa: ANN001
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """1.23: отмена доставленного уведомления заказывает отзыв с кассы."""
        notification_id = create_notification(auth_client)["notification"]["id"]
        deliver(session, store, notification_id, sessions=1)

        response = auth_client.post(f"{NOTIFICATIONS_URL}/{notification_id}/cancel")

        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "canceled"
        assert body["revoked_jobs"] == 1, "кассе, где уведомление уже показано, нужен отзыв"

        revoked = list(
            session.scalars(
                select(Command).where(Command.kind == str(CommandKind.NOTIFY_CANCEL))
            )
        )
        assert len(revoked) == 1
        assert revoked[0].params == {"notification_id": notification_id}
        assert revoked[0].store_id == store.id
        assert revoked[0].status == str(CommandStatus.QUEUED), "диспетчер доставит отзыв"

        # Повторная отмена — 409, лишних отзывов не появляется
        auth_client.post(f"{NOTIFICATIONS_URL}/{notification_id}/cancel")
        assert len(
            list(
                session.scalars(
                    select(Command).where(Command.kind == str(CommandKind.NOTIFY_CANCEL))
                )
            )
        ) == 1


class TestNotifyAction:
    """Действие `notify` из карточки магазина и массово (§5.2.2, §11.4)."""

    def test_store_action_creates_notification_and_job(  # noqa: ANN001
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        response = auth_client.post(
            f"/api/v1/admin/stores/{store.id}/actions",
            json={
                "action": "notify",
                "params": {
                    "title": "Проверка кассы",
                    "body": "Не выключайте кассу 10 минут",
                    "severity": "critical",
                    "requires_ack": True,
                },
            },
        )

        assert response.status_code == 202, response.text
        job = response.json()["job"]
        notification = notification_service.list_notifications(session)[0]

        assert job["kind"] == str(CommandKind.NOTIFY)
        assert job["params"] == {"notification_id": notification.id}
        assert notification.target_value == json.dumps([store.id])
        assert notification.severity == "critical"
        assert notification.requires_ack is True

    def test_bulk_action_targets_the_selection(  # noqa: ANN001
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        second = add_store(session, name="Магазин №10", ip_address="10.10.5.15", token="token-10")

        response = auth_client.post(
            "/api/v1/admin/bulk/actions",
            json={
                "action": "notify",
                "params": {"title": "Общее уведомление", "body": "Проверьте ленту"},
                "store_ids": [store.id, second.id],
            },
        )

        assert response.status_code == 202, response.text
        notifications = notification_service.list_notifications(session)
        assert len(notifications) == 1, "одно уведомление на всю выборку (§11.5)"
        assert {target.store_id for target in notifications[0].targets} == {
            store.id,
            second.id,
        }
        assert len(notify_jobs(session)) == 2


class TestNotifyDelivery:
    """Диспетчер доставляет уведомление и отмечает факт и сессии (§4.7, §11.4)."""

    def test_dispatcher_sends_notify_and_marks_delivered(  # noqa: ANN001
        self,
        session: Session,
        store: Store,
        settings: Settings,
        db_manager: DatabaseManager,
    ) -> None:
        settings_service.ensure_defaults(session)
        store.last_seen_at = utcnow()
        session.commit()
        notification = notification_service.create_notification(
            session,
            title="Смена закрывается в 22:00",
            body="Сохраните смену и отключите кассу",
            severity="critical",
            stores=[store],
            created_by="tester",
        )
        command_service.create_command(
            session,
            store_id=store.id,
            kind=CommandKind.NOTIFY,
            params={"notification_id": notification.id},
            created_by="tester",
        )
        session.commit()

        agent = ScriptedAgent()
        agent.sessions_online = 2
        dispatcher = CommandDispatcher(
            db_manager, settings, transport=agent.transport, interval_override_sec=0.05
        )

        report = asyncio.run(dispatcher.process_once())

        assert "/notify" in agent.paths(), "уведомление ушло на кассу (§11.4)"
        payload = agent.notify_pushes[0]
        assert payload["id"] == notification.id
        assert payload["snooze_min"] == 15, "интервал возврата задаёт сервер (§4.9, П-2)"
        assert payload["requires_ack"] is False
        assert report.succeeded == 1

        session.expire_all()
        target = session.scalars(
            select(NotificationTarget).where(
                NotificationTarget.notification_id == notification.id
            )
        ).one()
        assert target.delivered_at is not None
        assert target.sessions_count == 2, "число сессий кассы попадает в «Y» (§4.7)"
        delivered = session.scalars(
            select(Command).where(Command.kind == str(CommandKind.NOTIFY))
        ).one()
        assert delivered.status == str(CommandStatus.SUCCESS)

    def test_dispatcher_sends_notify_cancel(  # noqa: ANN001
        self,
        session: Session,
        store: Store,
        settings: Settings,
        db_manager: DatabaseManager,
    ) -> None:
        """1.23: диспетчер доставляет отзыв `notify_cancel` на кассу."""
        settings_service.ensure_defaults(session)
        store.last_seen_at = utcnow()
        session.commit()
        notification = notification_service.create_notification(
            session,
            title="Устарело",
            body="Отменить рассылку",
            severity="info",
            stores=[store],
            created_by="tester",
        )
        command_service.create_command(
            session,
            store_id=store.id,
            kind=CommandKind.NOTIFY_CANCEL,
            params={"notification_id": notification.id},
            created_by="tester",
        )
        session.commit()

        agent = ScriptedAgent()
        dispatcher = CommandDispatcher(
            db_manager, settings, transport=agent.transport, interval_override_sec=0.05
        )

        report = asyncio.run(dispatcher.process_once())

        assert "/notify/cancel" in agent.paths(), "отзыв ушёл на кассу (§7.4)"
        assert agent.notify_cancel_pushes == [{"notification_id": notification.id}]
        assert report.succeeded == 1
        delivered = session.scalars(
            select(Command).where(Command.kind == str(CommandKind.NOTIFY_CANCEL))
        ).one()
        assert delivered.status == str(CommandStatus.SUCCESS)






class TestDeleteNotification:
    """Э10: удаление уведомления целиком — задачи, отметки, адресаты (§11.5)."""

    def test_delete_closes_jobs_and_hides_notification(  # noqa: ANN001
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        notification_id = create_notification(auth_client)["notification"]["id"]

        response = auth_client.delete(f"{NOTIFICATIONS_URL}/{notification_id}")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["notification_id"] == notification_id
        assert body["canceled_jobs"] == 1, "незавершённая задача рассылки закрывается"
        assert "Уведомление удалено" in body["message"]
        assert auth_client.get(f"{NOTIFICATIONS_URL}").json()["items"] == []
        assert auth_client.get(f"{NOTIFICATIONS_URL}/{notification_id}").status_code == 404
        assert all(
            job.status == str(CommandStatus.CANCELED) for job in notify_jobs(session)
        ), "офлайн-касса не получит удалённое уведомление"
        actions = [row.action for row in session.scalars(select(AuditLog)).all()]
        assert "notification_delete" in actions
        assert auth_client.delete(f"{NOTIFICATIONS_URL}/{notification_id}").status_code == 404

    def test_delete_revokes_delivered_store(  # noqa: ANN001
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """1.23: удаление доставленного уведомления заказывает отзыв с кассы."""
        notification_id = create_notification(auth_client)["notification"]["id"]
        deliver(session, store, notification_id, sessions=1)

        response = auth_client.delete(f"{NOTIFICATIONS_URL}/{notification_id}")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["revoked_jobs"] == 1, "доставленной кассе нужен отзыв при удалении"
        assert "кассам отправлен отзыв" in body["message"]

        revoked = list(
            session.scalars(
                select(Command).where(Command.kind == str(CommandKind.NOTIFY_CANCEL))
            )
        )
        assert len(revoked) == 1
        assert revoked[0].params == {"notification_id": notification_id}
        assert revoked[0].store_id == store.id

    def test_delete_revokes_even_when_not_delivered(  # noqa: ANN001
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """1.24: удаление отзывается и кассе без `delivered_at` — иначе уведомление,
        которое уже дошло (подтверждение доставки потерялось), «повисло» бы в трее."""
        notification_id = create_notification(auth_client)["notification"]["id"]

        response = auth_client.delete(f"{NOTIFICATIONS_URL}/{notification_id}")

        assert response.status_code == 200, response.text
        assert response.json()["revoked_jobs"] == 1, "отзыв нужен всем адресатам"
        revoked = list(
            session.scalars(
                select(Command).where(Command.kind == str(CommandKind.NOTIFY_CANCEL))
            )
        )
        assert revoked[0].store_id == store.id


CATEGORIES_URL = "/api/v1/admin/notification-categories"


def create_category(client: TestClient, **overrides: object) -> dict:
    """Завести категорию через API — так же, как это делает админ (1.22, §11.5)."""
    payload: dict = {"name": "Продажи", "color": "#2563eb", "sort_order": 1}
    payload.update(overrides)
    response = client.post(CATEGORIES_URL, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


class TestNotificationCategories:
    """Категории уведомлений (спека 1.22, §11.5): CRUD, проверки и привязка."""

    def test_create_and_list_categories(self, auth_client: TestClient) -> None:
        """Категория создаётся с цветом из палитры и видна в списке (§11.5, 1.22)."""
        created = create_category(auth_client)

        assert created["name"] == "Продажи"
        assert created["color"] == "#2563eb"
        assert created["is_active"] is True

        items = auth_client.get(CATEGORIES_URL).json()
        assert [item["name"] for item in items] == ["Продажи"]

    def test_duplicate_name_is_conflict(self, auth_client: TestClient) -> None:
        """Дубль имени — `409`: админ не заводит вторую «Продажи» (§11.5)."""
        create_category(auth_client, name="Закупки")

        response = auth_client.post(CATEGORIES_URL, json={"name": "закупки"})

        assert response.status_code == 409, response.text
        assert "уже есть" in response.json()["detail"]

    def test_unknown_color_is_rejected(self, auth_client: TestClient) -> None:
        """Цвет — только из палитры: плашки читаются и на тёмном, и на светлом (§11.5)."""
        response = auth_client.post(
            CATEGORIES_URL, json={"name": "Продажи", "color": "#123456"}
        )

        assert response.status_code == 422, response.text

    def test_update_category(self, auth_client: TestClient) -> None:
        """Категорию можно переименовать и сменить цвет (§11.5, 1.22)."""
        created = create_category(auth_client)

        response = auth_client.put(
            f"{CATEGORIES_URL}/{created['id']}",
            json={"name": "Продажи и маркетинг", "color": "#10b981"},
        )

        assert response.status_code == 200, response.text
        updated = response.json()
        assert updated["name"] == "Продажи и маркетинг"
        assert updated["color"] == "#10b981"

    def test_soft_delete_hides_from_active_list(self, auth_client: TestClient) -> None:
        """Мягкое удаление: категория уходит из выбора, но остаётся в справочнике (§11.5)."""
        created = create_category(auth_client)

        response = auth_client.delete(f"{CATEGORIES_URL}/{created['id']}")

        assert response.status_code == 200, response.text
        assert auth_client.get(CATEGORIES_URL).json() == []
        all_items = auth_client.get(CATEGORIES_URL, params={"include_inactive": True}).json()
        assert [item["name"] for item in all_items] == ["Продажи"]
        assert all_items[0]["is_active"] is False

    def test_delete_missing_category_is_404(self, auth_client: TestClient) -> None:
        """Неизвестная категория — `404` (§11.5)."""
        assert auth_client.delete(f"{CATEGORIES_URL}/999").status_code == 404

    def test_notification_carries_category(  # noqa: ANN001
        self, auth_client: TestClient, store: Store
    ) -> None:
        """Уведомление создаётся с категорией, и она видна в списке (§11.5, 1.22)."""
        category = create_category(auth_client)

        created = create_notification(
            auth_client,
            category_id=category["id"],
            target_type="stores",
            store_ids=[store.id],
        )

        assert created["notification"]["category"]["name"] == "Продажи"
        assert created["notification"]["category"]["color"] == "#2563eb"

        items = auth_client.get(NOTIFICATIONS_URL).json()["items"]
        assert items[0]["category"]["id"] == category["id"]

    def test_notification_without_category(  # noqa: ANN001
        self, auth_client: TestClient, store: Store
    ) -> None:
        """Без категории уведомление создаётся как раньше: `category=null` (§11.5)."""
        created = create_notification(
            auth_client, target_type="stores", store_ids=[store.id]
        )

        assert created["notification"]["category"] is None

    def test_push_payload_brings_category_to_agent(  # noqa: ANN001
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """Категория уезжает на кассу вместе с уведомлением (§7.4, 1.22)."""
        category = create_category(auth_client)
        created = create_notification(
            auth_client,
            category_id=category["id"],
            target_type="stores",
            store_ids=[store.id],
        )
        notification = notification_service.get_notification(
            session, created["notification"]["id"]
        )

        payload = notification_service.push_payload(session, store, notification)

        assert payload.category is not None
        assert payload.category.name == "Продажи"
        assert payload.category.color == "#2563eb"

    def test_categories_require_authorization(self, make_client) -> None:  # noqa: ANN001
        """Эндпоинт защищён по умолчанию: без сессии — `401` (§8.1)."""
        client = make_client()

        assert client.get(CATEGORIES_URL).status_code == 401
        assert client.post(CATEGORIES_URL, json={"name": "Продажи"}).status_code == 401

    def test_create_records_audit(self, auth_client: TestClient, session: Session) -> None:
        """Создание категории попадает в аудит (§4.10)."""
        create_category(auth_client)

        actions = [row.action for row in session.scalars(select(AuditLog)).all()]
        assert "notification_category_create" in actions

