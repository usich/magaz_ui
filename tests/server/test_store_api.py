"""Тесты API магазинов (§5.2.2, §11.2) и кнопки «Проверить сейчас» (Д-3)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from server.db.models import AuditLog, Store
from tests.server.agent_mock import health_body, ok_transport, status_transport, timeout_transport
from tests.server.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, STORE_IP

STORES_URL = "/api/v1/admin/stores"
LOGIN_URL = "/api/v1/admin/auth/login"
SYSTEM_INFO_URL = "/api/v1/admin/system/info"
AGENT_HEALTH_URL = "/api/v1/agent/health"

#: Тело создания магазина по колонкам ТЗ (§11.2)
CREATE_PAYLOAD: dict[str, object] = {
    "name": "Магазин №12",
    "ip_address": "10.10.12.12",
    "cluster_name": "srv-1c",
    "database_name": "trade",
    "version_1c": "8.3.24.1548",
    "user_name_1c": "Администратор",
    "extension_name": "БГ_Торговоя",
}


def login(client: TestClient) -> TestClient:
    """Войти в дашборд — фабрика `make_client` отдаёт клиент без сессии."""
    response = client.post(
        LOGIN_URL, json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return client


def audit_actions(engine: Engine) -> list[str]:
    """Действия журнала аудита, от свежих к старым."""
    with Session(engine) as check:
        rows = check.scalars(select(AuditLog).order_by(AuditLog.id.desc())).all()
        return [row.action for row in rows]


class TestStoreApiAuth:
    """Все точки магазинов закрыты сессией администратора (§8.1)."""

    def test_list_requires_auth(self, client: TestClient) -> None:
        assert client.get(STORES_URL).status_code == 401

    def test_create_requires_auth(self, client: TestClient) -> None:
        assert client.post(STORES_URL, json=CREATE_PAYLOAD).status_code == 401

    def test_store_scoped_endpoints_require_auth(self, client: TestClient, store) -> None:  # noqa: ANN001
        assert client.get(f"{STORES_URL}/{store.id}").status_code == 401
        assert client.put(f"{STORES_URL}/{store.id}", json={"comment": "x"}).status_code == 401
        assert client.delete(f"{STORES_URL}/{store.id}").status_code == 401
        assert client.post(f"{STORES_URL}/{store.id}/token/rotate").status_code == 401
        assert client.put(f"{STORES_URL}/{store.id}/credentials", json={}).status_code == 401
        assert client.post(f"{STORES_URL}/{store.id}/check").status_code == 401
        assert client.get(f"{STORES_URL}/{store.id}/agent-config").status_code == 401


class TestStoreCrud:
    """CRUD по колонкам ТЗ (§11.2)."""

    def test_create_returns_store_and_agent_config(
        self, auth_client: TestClient, admin, engine: Engine  # noqa: ANN001, ARG002
    ) -> None:
        response = auth_client.post(STORES_URL, json=CREATE_PAYLOAD)
        body = response.json()

        assert response.status_code == 200, response.text
        assert body["name"] == "Магазин №12"
        assert body["is_online"] is False
        assert body["password_set"] is False
        assert body["credentials_version"] == 1
        assert body["devices_version"] == 1
        assert body["extension_name"] == "БГ_Торговоя", "имя расширения сохраняется (FR-4)"

        config = body["agent_config"]
        assert config["store_id"] == body["id"]
        assert config["agent_token"], "токен выдаётся при создании"
        assert config["config_path"].endswith("agent-config.json")
        assert config["server_url"].startswith("http")
        assert config["config_json"]["store_id"] == body["id"]
        assert config["config_json"]["agent_token"] == config["agent_token"]
        assert config["config_json"]["port"] == 8383
        assert audit_actions(engine)[0] == "store_create"

    def test_duplicate_name_is_422(self, auth_client: TestClient, admin, store) -> None:  # noqa: ANN001, ARG002
        response = auth_client.post(
            STORES_URL, json={**CREATE_PAYLOAD, "name": store.name.lower()}
        )
        assert response.status_code == 422
        assert "уже существует" in response.text

    def test_invalid_ip_is_422(self, auth_client: TestClient, admin) -> None:  # noqa: ANN001, ARG002
        response = auth_client.post(STORES_URL, json={**CREATE_PAYLOAD, "ip_address": "не-ip"})
        assert response.status_code == 422
        assert "IPv4" in response.text

    def test_get_unknown_is_404(self, auth_client: TestClient, admin) -> None:  # noqa: ANN001, ARG002
        assert auth_client.get(f"{STORES_URL}/4242").status_code == 404

    def test_get_returns_card_without_secrets(self, auth_client: TestClient, admin, store) -> None:  # noqa: ANN001, ARG002
        response = auth_client.get(f"{STORES_URL}/{store.id}")
        body = response.json()

        assert response.status_code == 200
        assert body["id"] == store.id
        assert body["ip_address"] == STORE_IP
        assert body["cluster_name"] == "srv-1c"
        assert "agent_token" not in body
        assert "password_enc" not in body

    def test_agent_config_available_at_any_time(
        self, auth_client: TestClient, admin, store  # noqa: ANN001, ARG002
    ) -> None:
        """Конфиг агента перечитывается в любой момент, а не только при создании (§11.2).

        Токен здесь отдаётся: это тот же блок, что и в ответе на создание, а роут
        закрыт сессией администратора (`require_active_admin`).
        """
        response = auth_client.get(f"{STORES_URL}/{store.id}/agent-config")
        config = response.json()

        assert response.status_code == 200
        assert config["store_id"] == store.id
        assert config["agent_token"] == store.agent_token
        assert config["port"] == store.agent_port
        assert config["config_path"].endswith("agent-config.json")
        assert config["server_url"].startswith("http")
        assert config["config_json"]["store_id"] == store.id
        assert config["config_json"]["store_name"] == store.name
        assert config["config_json"]["agent_token"] == store.agent_token
        assert config["config_json"]["one_c_profile"]["user_name"] == store.user_name_1c
        assert config["config_json"]["one_c_profile"]["password_enc"] is None, "П-1"
        assert config["notes"], "подсказки по установке остаются в блоке"

    def test_agent_config_of_unknown_store_is_404(
        self, auth_client: TestClient, admin  # noqa: ANN001, ARG002
    ) -> None:
        assert auth_client.get(f"{STORES_URL}/4242/agent-config").status_code == 404

    def test_recreate_after_deactivate_reuses_ip_and_name(
        self, auth_client: TestClient, admin  # noqa: ANN001, ARG002
    ) -> None:
        """Отключённый магазин не занимает имя и IP (§5.2.2, дефект Э8)."""
        first = auth_client.post(STORES_URL, json=CREATE_PAYLOAD).json()
        assert auth_client.delete(f"{STORES_URL}/{first['id']}").status_code == 200

        again = auth_client.post(STORES_URL, json=CREATE_PAYLOAD)

        assert again.status_code == 200, again.text
        body = again.json()
        assert body["id"] != first["id"], "создаётся новая запись, история прежней цела"
        assert body["ip_address"] == CREATE_PAYLOAD["ip_address"]
        assert body["name"] == CREATE_PAYLOAD["name"]
        assert body["is_active"] is True

    def test_reactivate_with_taken_ip_is_422(
        self, auth_client: TestClient, admin  # noqa: ANN001, ARG002
    ) -> None:
        """Включить обратно магазин, чьи имя/IP занял активный, — понятная `422`."""
        first = auth_client.post(STORES_URL, json=CREATE_PAYLOAD).json()
        auth_client.delete(f"{STORES_URL}/{first['id']}")
        second = auth_client.post(STORES_URL, json=CREATE_PAYLOAD).json()

        response = auth_client.put(f"{STORES_URL}/{first['id']}", json={"is_active": True})

        assert response.status_code == 422
        assert "уже существует" in response.text
        assert auth_client.get(f"{STORES_URL}/{first['id']}").json()["is_active"] is False
        assert auth_client.get(f"{STORES_URL}/{second['id']}").json()["is_active"] is True

    def test_update_changes_fields_and_writes_audit(
        self, auth_client: TestClient, admin, store, engine: Engine  # noqa: ANN001
    ) -> None:
        response = auth_client.put(
            f"{STORES_URL}/{store.id}",
            json={"version_1c": "8.3.25.1", "comment": "перенесли кассу"},
        )
        body = response.json()

        assert response.status_code == 200
        assert body["version_1c"] == "8.3.25.1"
        assert body["comment"] == "перенесли кассу"
        with Session(engine) as check:
            assert check.get(Store, store.id).version_1c == "8.3.25.1"
        assert audit_actions(engine)[0] == "store_update"

    def test_update_login_bumps_credentials_version(
        self, auth_client: TestClient, admin, store  # noqa: ANN001, ARG002
    ) -> None:
        before = store.credentials_version
        response = auth_client.put(
            f"{STORES_URL}/{store.id}", json={"user_name_1c": "ОбменДанными"}
        )
        assert response.json()["credentials_version"] == before + 1

    def test_delete_is_soft(self, auth_client: TestClient, admin, store, engine: Engine) -> None:  # noqa: ANN001
        response = auth_client.delete(f"{STORES_URL}/{store.id}")

        assert response.status_code == 200
        assert response.json()["is_active"] is False
        with Session(engine) as check:
            assert check.get(Store, store.id) is not None, "строка остаётся в БД"
            assert check.get(Store, store.id).is_active is False

        listed = [item["id"] for item in auth_client.get(STORES_URL).json()]
        assert store.id not in listed
        listed_all = [
            item["id"]
            for item in auth_client.get(
                STORES_URL, params={"include_inactive": True}
            ).json()
        ]
        assert store.id in listed_all
        assert audit_actions(engine)[0] == "store_deactivate"

    def test_status_filter_and_search(
        self, auth_client: TestClient, admin, store, engine: Engine  # noqa: ANN001
    ) -> None:
        with Session(engine) as change:
            change.get(Store, store.id).last_seen_at = None
            change.commit()

        assert auth_client.get(STORES_URL, params={"status": "offline"}).json()[0]["id"] == store.id
        assert auth_client.get(STORES_URL, params={"status": "online"}).json() == []
        assert auth_client.get(STORES_URL, params={"search": "магазин"}).json()[0]["id"] == store.id
        assert auth_client.get(STORES_URL, params={"status": "broken"}).status_code == 422


class TestCredentialsApi:
    """Учётные данные 1С (П-1): значение пароля не покидает сервер (§8.3)."""

    def test_set_password_marks_it_set(
        self, auth_client: TestClient, admin, store, engine: Engine  # noqa: ANN001
    ) -> None:
        before = store.credentials_version
        response = auth_client.put(
            f"{STORES_URL}/{store.id}/credentials",
            json={"user_name_1c": "ОбменДанными", "password": "Секрет1С"},
        )
        body = response.json()

        assert response.status_code == 200
        assert body["password_set"] is True
        assert body["user_name_1c"] == "ОбменДанными"
        assert body["credentials_version"] == before + 1
        assert "Секрет1С" not in response.text, "I-7: пароль не возвращается"

        with Session(engine) as check:
            assert check.get(Store, store.id).password_enc is not None
        assert audit_actions(engine)[0] == "store_credentials_update"

    def test_clear_password(
        self, auth_client: TestClient, admin, store  # noqa: ANN001, ARG002
    ) -> None:
        auth_client.put(
            f"{STORES_URL}/{store.id}/credentials", json={"password": "Секрет1С"}
        )
        response = auth_client.put(
            f"{STORES_URL}/{store.id}/credentials", json={"clear_password": True}
        )
        assert response.json()["password_set"] is False

    def test_password_absent_from_api_and_audit(
        self, auth_client: TestClient, admin, store, engine: Engine  # noqa: ANN001
    ) -> None:
        auth_client.put(
            f"{STORES_URL}/{store.id}/credentials", json={"password": "Секрет1С"}
        )
        card = auth_client.get(f"{STORES_URL}/{store.id}")
        audit = auth_client.get("/api/v1/admin/audit", params={"limit": 20})

        assert "Секрет1С" not in card.text
        assert "Секрет1С" not in audit.text
        with Session(engine) as check:
            entry = check.scalars(select(AuditLog).order_by(AuditLog.id.desc())).first()
            assert entry.action == "store_credentials_update"
            assert entry.details["password_set"] is True


class TestTokenRotation:
    """Перевыпуск токена: старый токен обязан перестать работать (§11.2)."""

    def test_rotate_invalidates_old_token(
        self, auth_client: TestClient, admin, store, engine: Engine  # noqa: ANN001
    ) -> None:
        old_token = store.agent_token
        response = auth_client.post(f"{STORES_URL}/{store.id}/token/rotate")
        body = response.json()

        assert response.status_code == 200
        assert body["agent_token"] != old_token
        assert "перенастроить" in body["warning"]
        assert audit_actions(engine)[0] == "store_token_rotate"

        # Старый токен больше не действует в Agent API (§8.1)
        stale = auth_client.post(
            AGENT_HEALTH_URL,
            json={"store_id": store.id, "agent_version": "1.0.0"},
            headers={"X-Agent-Token": old_token, "X-Forwarded-For": STORE_IP},
        )
        assert stale.status_code == 401

        # Новый токен работает
        fresh = auth_client.post(
            AGENT_HEALTH_URL,
            json={"store_id": store.id, "agent_version": "1.0.0"},
            headers={"X-Agent-Token": body["agent_token"], "X-Forwarded-For": STORE_IP},
        )
        assert fresh.status_code == 200
        assert fresh.json()["store_id"] == store.id


class TestStoreCheck:
    """Кнопка «Проверить сейчас» (Д-3): живой `GET /health` агента."""

    def test_ok_updates_card_and_returns_live_data(
        self, make_client, admin, store, engine: Engine  # noqa: ANN001, ARG002
    ) -> None:
        client = login(make_client(ok_transport(health_body(store_id=store.id, os="Windows Server 2019"))))
        response = client.post(f"{STORES_URL}/{store.id}/check")
        body = response.json()

        assert response.status_code == 200, response.text
        assert body["status"] == "online"
        assert body["agent_version"] == "1.0.0"
        assert body["os"] == "Windows Server 2019"
        assert body["work_dir"] == r"C:\MagazAgent"
        assert body["base_1c_path"] == "C:/Program Files (x86)/1cv8"
        assert body["busy"] == {"merge": False, "update": False}
        assert body["sessions"] == [{"user": "user1", "sid": "1"}]
        assert body["checked_at"].endswith("Z")
        assert body["latency_ms"] is not None
        assert body["credentials_push_required"] is False
        assert body["devices_push_required"] is False

        # Карточка магазина обновилась — магазин стал «онлайн» (§3.3)
        card = client.get(f"{STORES_URL}/{store.id}").json()
        assert card["is_online"] is True
        assert card["agent_version"] == "1.0.0"
        assert card["os_info"] == "Windows Server 2019"
        assert card["last_seen_at"].endswith("Z")

    def test_version_mismatch_asks_for_push(
        self, make_client, admin, store  # noqa: ANN001, ARG002
    ) -> None:
        transport = ok_transport(
            health_body(store_id=store.id, credentials_version=0, devices_version=0)
        )
        client = login(make_client(transport))
        body = client.post(f"{STORES_URL}/{store.id}/check").json()

        assert body["credentials_push_required"] is True, "П-1: пароль 1С нужно дослать"
        assert body["devices_push_required"] is True, "Д-1: список оборудования нужно дослать"

    def test_offline_agent_is_503_and_store_stays_offline(
        self, make_client, admin, store  # noqa: ANN001, ARG002
    ) -> None:
        client = login(make_client(timeout_transport()))
        response = client.post(f"{STORES_URL}/{store.id}/check")

        assert response.status_code == 503
        assert "офлайн" in response.text
        assert client.get(f"{STORES_URL}/{store.id}").json()["is_online"] is False

    def test_agent_rejecting_token_is_502(
        self, make_client, admin, store  # noqa: ANN001, ARG002
    ) -> None:
        client = login(make_client(status_transport(401, {"detail": "Неверный токен"})))
        response = client.post(f"{STORES_URL}/{store.id}/check")

        assert response.status_code == 502
        assert "токен" in response.text

    def test_unknown_store_is_404(self, make_client, admin) -> None:  # noqa: ANN001, ARG002
        client = login(make_client(ok_transport()))
        assert client.post(f"{STORES_URL}/4242/check").status_code == 404


class TestSystemCounters:
    """Плитки главной: «Онлайн N / Офлайн M / Всего» (§11.1, FR-13)."""

    def test_counts_reflect_monitoring(
        self, auth_client: TestClient, admin, store, engine: Engine  # noqa: ANN001
    ) -> None:
        counts = auth_client.get(SYSTEM_INFO_URL).json()["counts"]
        assert counts["stores_total"] == 1
        assert counts["stores_active"] == 1
        assert counts["stores_online"] == 0
        assert counts["stores_offline"] == 1
        assert counts["online_window_sec"] == 45

        with Session(engine) as change:
            from shared.timeutil import utcnow

            change.get(Store, store.id).last_seen_at = utcnow()
            change.commit()

        counts = auth_client.get(SYSTEM_INFO_URL).json()["counts"]
        assert counts["stores_online"] == 1
        assert counts["stores_offline"] == 0