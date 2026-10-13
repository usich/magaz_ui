"""Тесты самодокладов агента `/api/v1/agent/*` (§5.2.2, §8.1) — состав Э1."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from server.db.models import Store
from shared.timeutil import utcnow

HEALTH_URL = "/api/v1/agent/health"


def health_payload(**overrides) -> dict:  # noqa: ANN003
    """Тело самодоклада: агент сообщает версию, ОС и версии доставленных данных."""
    payload = {
        "store_id": 1,
        "agent_version": "1.0.0",
        "os": "Windows 10 Pro 22H2",
        "work_dir": r"C:\MagazAgent",
        "base_1c_path": "C:/Program Files (x86)/1cv8",
        "credentials_version": 1,
        "devices_version": 1,
        "busy": {"merge": False, "update": False},
        "sessions": [{"user": "user1", "sid": "1"}],
    }
    payload.update(overrides)
    return payload


class TestAgentAuth:
    """FR-11, §8.1: токен агента + контроль IP источника."""

    def test_missing_token_is_401(self, client: TestClient, store) -> None:  # noqa: ANN001, ARG002
        response = client.post(HEALTH_URL, json=health_payload(store_id=store.id))
        assert response.status_code == 401

    def test_unknown_token_is_401(self, client: TestClient, store) -> None:  # noqa: ANN001, ARG002
        response = client.post(
            HEALTH_URL,
            json=health_payload(store_id=store.id),
            headers={"X-Agent-Token": "stolen-token", "X-Forwarded-For": store.ip_address},
        )
        assert response.status_code == 401

    def test_wrong_source_ip_is_403(
        self, client: TestClient, store, agent_headers: dict[str, str]  # noqa: ANN001
    ) -> None:
        response = client.post(
            HEALTH_URL,
            json=health_payload(store_id=store.id),
            headers={**agent_headers, "X-Forwarded-For": "10.99.99.99"},
        )
        assert response.status_code == 403

    def test_inactive_store_is_403(
        self, client: TestClient, store, agent_headers: dict[str, str], engine: Engine, session: Session  # noqa: ANN001
    ) -> None:
        session.get(Store, store.id).is_active = False
        session.commit()

        response = client.post(
            HEALTH_URL, json=health_payload(store_id=store.id), headers=agent_headers
        )
        assert response.status_code == 403


class TestAgentSelfReport:
    """Самодоклад обновляет карточку магазина и сообщает, что нужно дослать."""

    def test_updates_store_card(
        self, client: TestClient, store, agent_headers: dict[str, str], engine: Engine  # noqa: ANN001
    ) -> None:
        response = client.post(
            HEALTH_URL,
            json=health_payload(store_id=store.id),
            headers=agent_headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["store_id"] == store.id
        assert body["server_time"].endswith("Z")
        assert body["credentials_push_required"] is False
        assert body["devices_push_required"] is False

        with Session(engine) as check:
            refreshed = check.get(Store, store.id)
            assert refreshed.agent_version == "1.0.0"
            assert refreshed.os_info == "Windows 10 Pro 22H2"
            assert refreshed.last_seen_at is not None
            assert refreshed.is_online(window_sec=45) is True

    def test_version_mismatch_requests_push(
        self, client: TestClient, store, agent_headers: dict[str, str]  # noqa: ANN001
    ) -> None:
        response = client.post(
            HEALTH_URL,
            json=health_payload(store_id=store.id, credentials_version=0, devices_version=0),
            headers=agent_headers,
        )

        body = response.json()
        assert body["credentials_push_required"] is True, "П-1: пароль 1С нужно дослать"
        assert body["devices_push_required"] is True, "Д-1: список оборудования нужно дослать"

    def test_foreign_store_id_is_ignored(
        self, client: TestClient, store, agent_headers: dict[str, str], engine: Engine  # noqa: ANN001
    ) -> None:
        response = client.post(
            HEALTH_URL, json=health_payload(store_id=9999), headers=agent_headers
        )

        assert response.status_code == 200
        assert response.json()["store_id"] == store.id, "источник истины — токен, а не тело"
        with Session(engine) as check:
            assert check.query(Store).count() == 1

    def test_last_seen_refresh_moves_store_online(
        self, client: TestClient, store, agent_headers: dict[str, str], engine: Engine, session: Session  # noqa: ANN001
    ) -> None:
        stale = session.get(Store, store.id)
        stale.last_seen_at = utcnow() - timedelta(minutes=10)
        session.commit()

        client.post(HEALTH_URL, json=health_payload(store_id=store.id), headers=agent_headers)

        with Session(engine) as check:
            assert check.get(Store, store.id).is_online(window_sec=45) is True
