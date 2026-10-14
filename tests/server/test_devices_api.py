"""Тесты API оборудования подсети (§5.2.2, §11.7) и батча опроса (Д-1, Э4Б).

Сеть не нужна: сервер обращается к кассе через `ScriptedAgent` (§13), а
«Проверить сейчас» проверяется на подменённом транспорте.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from server.db.models import AuditLog, Store, StoreDevice
from tests.server.agent_mock import ScriptedAgent, status_transport, timeout_transport
from tests.server.conftest import ADMIN_PASSWORD, ADMIN_USERNAME

DEVICES_URL = "/api/v1/admin/devices"
STORES_URL = "/api/v1/admin/stores"
LOGIN_URL = "/api/v1/admin/auth/login"
SYSTEM_INFO_URL = "/api/v1/admin/system/info"
AGENT_STATUS_URL = "/api/v1/agent/devices/status"


def login(client: TestClient) -> TestClient:
    """Войти в дашборд — фабрика `make_client` отдаёт клиент без сессии."""
    response = client.post(
        LOGIN_URL, json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return client


def audit_actions(engine: Engine) -> list[str]:
    """Действия журнала аудита, от свежих к старым (§4.10)."""
    with Session(engine) as check:
        rows = check.scalars(select(AuditLog).order_by(AuditLog.id.desc())).all()
        return [row.action for row in rows]


def add_device(
    client: TestClient, store_id: int, **overrides: object
) -> dict:
    """Завести устройство через API — так же, как это делает админ (§11.7)."""
    payload: dict = {"name": "камера вход", "ip_address": "10.10.1.21"}
    payload.update(overrides)
    response = client.post(f"{STORES_URL}/{store_id}/devices", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


class TestDevicesApiAuth:
    """Все точки оборудования закрыты сессией администратора (§8.1)."""

    def test_endpoints_require_auth(self, client: TestClient, store) -> None:  # noqa: ANN001
        assert client.get(f"{STORES_URL}/{store.id}/devices").status_code == 401
        assert client.post(f"{STORES_URL}/{store.id}/devices", json={}).status_code == 401
        assert client.get(f"{STORES_URL}/{store.id}/devices/1").status_code == 401
        assert client.put(f"{STORES_URL}/{store.id}/devices/1", json={}).status_code == 401
        assert client.delete(f"{STORES_URL}/{store.id}/devices/1").status_code == 401
        assert client.post(f"{STORES_URL}/{store.id}/devices/check").status_code == 401
        assert client.get(DEVICES_URL + "/matrix").status_code == 401

    def test_agent_status_requires_token(self, client: TestClient) -> None:
        assert client.post(AGENT_STATUS_URL, json={"store_id": 1, "devices": []}).status_code == 401


class TestDevicesCrud:
    """CRUD списка оборудования и версия списка (§4.11, §7.6)."""

    def test_create_device_bumps_version_and_writes_audit(
        self, auth_client: TestClient, session: Session, store, engine: Engine  # noqa: ANN001
    ) -> None:
        before = store.devices_version

        body = add_device(auth_client, store.id)

        assert body["name"] == "камера вход"
        assert body["ip_address"] == "10.10.1.21"
        assert body["check_type"] == "icmp"
        assert body["check_port"] is None
        assert body["is_active"] is True
        assert body["last_status"] is None, "статуса ещё нет — в UI это `unknown` (§4.11)"
        assert body["store_name"] == store.name
        session.expire_all()
        assert store.devices_version == before + 1, "изменение списка — новая версия (§7.6)"
        assert audit_actions(engine)[0] == "device_create"

    def test_tcp_requires_port_and_valid_type(
        self, auth_client: TestClient, store  # noqa: ANN001
    ) -> None:
        bad_port = auth_client.post(
            f"{STORES_URL}/{store.id}/devices",
            json={"name": "камера", "ip_address": "10.10.1.40", "check_type": "tcp"},
        )
        bad_type = auth_client.post(
            f"{STORES_URL}/{store.id}/devices",
            json={"name": "камера", "ip_address": "10.10.1.41", "device_type": "router"},
        )
        bad_ip = auth_client.post(
            f"{STORES_URL}/{store.id}/devices",
            json={"name": "камера", "ip_address": "10.10.1.0/24"},
        )

        assert bad_port.status_code == 422 and "порт" in bad_port.json()["detail"].lower()
        assert bad_type.status_code == 422
        assert bad_ip.status_code == 422

    def test_duplicate_ip_in_store_is_422(
        self, auth_client: TestClient, store  # noqa: ANN001
    ) -> None:
        add_device(auth_client, store.id)

        again = auth_client.post(
            f"{STORES_URL}/{store.id}/devices",
            json={"name": "камера склад", "ip_address": "10.10.1.21"},
        )

        assert again.status_code == 422
        assert "уже заведено" in again.json()["detail"]

    def test_same_ip_in_another_store_is_allowed(
        self, auth_client: TestClient, session: Session, store  # noqa: ANN001
    ) -> None:
        """§4.11: уникальна пара «магазин + IP», а не сам адрес."""
        second = Store(
            name="Магазин №6",
            ip_address="10.10.6.10",
            cluster_name="srv-1c",
            database_name="trade2",
            version_1c="8.3.24.1548",
            user_name_1c="Администратор",
            agent_token="token-second-store",
        )
        session.add(second)
        session.commit()

        add_device(auth_client, store.id)
        body = add_device(auth_client, second.id)

        assert body["ip_address"] == "10.10.1.21"
        assert body["store_id"] == second.id


    def test_update_renames_and_disables(
        self, auth_client: TestClient, session: Session, store  # noqa: ANN001
    ) -> None:
        device = add_device(auth_client, store.id)
        session.expire_all()
        after_create = store.devices_version

        renamed = auth_client.put(
            f"{STORES_URL}/{store.id}/devices/{device['id']}", json={"name": "камера склад"}
        )
        disabled = auth_client.delete(f"{STORES_URL}/{store.id}/devices/{device['id']}")

        assert renamed.status_code == 200
        assert renamed.json()["name"] == "камера склад"
        assert disabled.status_code == 200
        assert disabled.json()["is_active"] is False, "мягкое удаление: §11.7"
        session.expire_all()
        assert store.devices_version == after_create + 2

    def test_device_of_another_store_is_404(
        self, auth_client: TestClient, session: Session, store  # noqa: ANN001
    ) -> None:
        second = Store(
            name="Магазин №7",
            ip_address="10.10.7.10",
            cluster_name="srv-1c",
            database_name="trade3",
            version_1c="8.3.24.1548",
            user_name_1c="Администратор",
            agent_token="token-third-store",
        )
        session.add(second)
        session.commit()
        device = add_device(auth_client, store.id)

        response = auth_client.get(f"{STORES_URL}/{second.id}/devices/{device['id']}")

        assert response.status_code == 404

    def test_list_can_hide_inactive(
        self, auth_client: TestClient, store  # noqa: ANN001
    ) -> None:
        active = add_device(auth_client, store.id)
        disabled = add_device(auth_client, store.id, name="принтер", ip_address="10.10.1.50")
        auth_client.delete(f"{STORES_URL}/{store.id}/devices/{disabled['id']}")

        full = auth_client.get(f"{STORES_URL}/{store.id}/devices").json()
        only_active = auth_client.get(
            f"{STORES_URL}/{store.id}/devices", params={"include_inactive": "false"}
        ).json()

        assert {item["id"] for item in full} == {active["id"], disabled["id"]}
        assert [item["id"] for item in only_active] == [active["id"]]



class TestAgentStatusBatch:
    """`POST /api/v1/agent/devices/status` — батч опроса от кассы (§5.2.2)."""

    def test_batch_updates_statuses_and_ignores_unknown_ips(
        self,
        client: TestClient,
        auth_client: TestClient,
        session: Session,
        store,  # noqa: ANN001
        agent_headers: dict[str, str],
    ) -> None:
        camera = add_device(auth_client, store.id)
        printer = add_device(auth_client, store.id, name="принтер", ip_address="10.10.1.50")

        response = client.post(
            AGENT_STATUS_URL,
            json={
                "store_id": store.id,
                "devices": [
                    {"ip": "10.10.1.21", "status": "online", "latency_ms": 12},
                    {"ip": "10.10.1.50", "status": "offline", "error": "нет ответа"},
                    {"ip": "10.10.99.99", "status": "online"},
                ],
            },
            headers=agent_headers,
        )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "ok": True,
            "store_id": store.id,
            "updated": 2,
            "ignored": 1,
            "agent_ok": True,
        }, "адрес, которого нет в списке, игнорируется — запрет sweep (§7.6)"

        session.expire_all()
        online = session.get(StoreDevice, camera["id"])
        offline = session.get(StoreDevice, printer["id"])
        assert online.last_status == "online"
        assert online.last_latency_ms == 12
        assert online.last_seen_at is not None
        assert online.is_online(window_sec=180) is True
        assert offline.last_status == "offline"
        assert offline.last_latency_ms is None, "у офлайн-устройства задержки нет"
        assert offline.last_error == "нет ответа"
        assert session.scalars(select(StoreDevice).where(StoreDevice.ip_address == "10.10.99.99")).all() == []

    def test_agent_ok_false_keeps_unknown_not_offline(
        self,
        client: TestClient,
        auth_client: TestClient,
        session: Session,
        store,  # noqa: ANN001
        agent_headers: dict[str, str],
    ) -> None:
        """§4.11: «нет связи с кассой» — это `unknown`, а не `offline`."""
        camera = add_device(auth_client, store.id)
        client.post(
            AGENT_STATUS_URL,
            json={
                "store_id": store.id,
                "devices": [{"ip": "10.10.1.21", "status": "online", "latency_ms": 5}],
            },
            headers=agent_headers,
        )
        session.expire_all()
        seen_at = session.get(StoreDevice, camera["id"]).last_seen_at
        assert seen_at is not None

        client.post(
            AGENT_STATUS_URL,
            json={
                "store_id": store.id,
                "agent_ok": False,
                "devices": [{"ip": "10.10.1.21", "status": "unknown"}],
            },
            headers=agent_headers,
        )

        session.expire_all()
        device = session.get(StoreDevice, camera["id"])
        assert device.last_status == "unknown"
        assert device.last_latency_ms is None
        assert device.last_seen_at == seen_at, "последний успешный ответ не переписываем"

    def test_store_id_from_body_is_not_trusted(
        self,
        client: TestClient,
        auth_client: TestClient,
        session: Session,
        store,  # noqa: ANN001
        agent_headers: dict[str, str],
    ) -> None:
        camera = add_device(auth_client, store.id)

        response = client.post(
            AGENT_STATUS_URL,
            json={
                "store_id": 999,
                "devices": [{"ip": "10.10.1.21", "status": "online", "latency_ms": 3}],
            },
            headers=agent_headers,
        )

        assert response.status_code == 200
        assert response.json()["store_id"] == store.id
        session.expire_all()
        assert session.get(StoreDevice, camera["id"]).last_status == "online"



class TestDevicesMatrix:
    """Сводная таблица `/devices` и плитка главной (§11.1, §11.7)."""

    def test_rows_and_counters(
        self,
        auth_client: TestClient,
        client: TestClient,
        store,  # noqa: ANN001
        agent_headers: dict[str, str],
    ) -> None:
        add_device(auth_client, store.id)
        add_device(
            auth_client,
            store.id,
            name="камера двор",
            ip_address="10.10.1.40",
            device_type="camera",
            check_type="tcp",
            check_port=554,
        )
        client.post(
            AGENT_STATUS_URL,
            json={
                "store_id": store.id,
                "devices": [{"ip": "10.10.1.21", "status": "online", "latency_ms": 9}],
            },
            headers=agent_headers,
        )

        body = auth_client.get(f"{DEVICES_URL}/matrix").json()

        assert body["total"] == 2
        assert body["online"] == 1
        assert body["offline"] == 0
        assert body["unknown"] == 1, "у второй камеры данных ещё не было (§4.11)"
        assert body["stores"] == 1
        assert body["window_sec"] == 180
        online_row = next(item for item in body["rows"] if item["is_online"])
        assert online_row["store_name"] == store.name
        assert online_row["cluster_name"] == "srv-1c"
        assert online_row["last_latency_ms"] == 9

    def test_filters_by_status_type_and_search(
        self,
        auth_client: TestClient,
        client: TestClient,
        store,  # noqa: ANN001
        agent_headers: dict[str, str],
    ) -> None:
        add_device(auth_client, store.id)
        add_device(
            auth_client, store.id, name="свитч склад", ip_address="10.10.1.30", device_type="switch"
        )
        client.post(
            AGENT_STATUS_URL,
            json={
                "store_id": store.id,
                "devices": [{"ip": "10.10.1.21", "status": "offline", "error": "нет ответа"}],
            },
            headers=agent_headers,
        )

        offline = auth_client.get(f"{DEVICES_URL}/matrix", params={"status": "offline"}).json()
        switches = auth_client.get(f"{DEVICES_URL}/matrix", params={"device_type": "switch"}).json()
        found = auth_client.get(f"{DEVICES_URL}/matrix", params={"search": "склад"}).json()
        unknown = auth_client.get(f"{DEVICES_URL}/matrix", params={"status": "unknown"}).json()

        assert [row["ip_address"] for row in offline["rows"]] == ["10.10.1.21"]
        assert [row["ip_address"] for row in switches["rows"]] == ["10.10.1.30"]
        assert [row["name"] for row in found["rows"]] == ["свитч склад"]
        assert [row["ip_address"] for row in unknown["rows"]] == ["10.10.1.30"]

    def test_bad_filter_is_422(self, auth_client: TestClient) -> None:
        assert auth_client.get(f"{DEVICES_URL}/matrix", params={"device_type": "router"}).status_code == 422

    def test_system_info_counts_devices(
        self,
        auth_client: TestClient,
        client: TestClient,
        store,  # noqa: ANN001
        agent_headers: dict[str, str],
    ) -> None:
        """Плитка главной «Устройства онлайн/всего» (§11.1)."""
        add_device(auth_client, store.id)
        add_device(auth_client, store.id, name="свитч", ip_address="10.10.1.30")
        client.post(
            AGENT_STATUS_URL,
            json={
                "store_id": store.id,
                "devices": [{"ip": "10.10.1.21", "status": "online", "latency_ms": 4}],
            },
            headers=agent_headers,
        )

        counts = auth_client.get(SYSTEM_INFO_URL).json()["counts"]

        assert counts["devices_total"] == 2
        assert counts["devices_online"] == 1
        assert counts["devices_window_sec"] == 180



class TestCheckNowEndpoint:
    """`POST /stores/{id}/devices/check` — «Проверить сейчас» (§11.7, Д-1)."""

    def test_agent_online_writes_results(
        self,
        make_client,  # noqa: ANN001
        admin,  # noqa: ANN001, ARG002
        session: Session,
        store,  # noqa: ANN001
    ) -> None:
        agent = ScriptedAgent(device_status="online", device_latency_ms=7)
        http = login(make_client(agent.transport))
        camera = add_device(http, store.id)

        response = http.post(f"{STORES_URL}/{store.id}/devices/check")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["agent_ok"] is True
        assert body["checked"] == 1
        assert body["updated"] == 1
        assert body["results"][0]["status"] == "online"
        assert agent.paths() == ["/devices/check-now"], "подсеть опрашивает касса, не сервер"
        assert agent.devices_checks[0][0] == {
            "ip": "10.10.1.21",
            "check_type": "icmp",
            "port": None,
        }
        session.expire_all()
        device = session.get(StoreDevice, camera["id"])
        assert device.last_status == "online"
        assert device.last_latency_ms == 7
        assert device.is_online(window_sec=180) is True

    def test_row_level_check_sends_only_selected(
        self,
        make_client,  # noqa: ANN001
        admin,  # noqa: ANN001, ARG002
        store,  # noqa: ANN001
    ) -> None:
        agent = ScriptedAgent()
        http = login(make_client(agent.transport))
        add_device(http, store.id)
        second = add_device(http, store.id, name="принтер", ip_address="10.10.1.50")

        response = http.post(
            f"{STORES_URL}/{store.id}/devices/check", json={"device_ids": [second["id"]]}
        )

        assert response.status_code == 200
        assert [item["ip"] for item in agent.devices_checks[0]] == ["10.10.1.50"]
        assert response.json()["checked"] == 1

    def test_offline_agent_marks_devices_unknown(
        self,
        make_client,  # noqa: ANN001
        admin,  # noqa: ANN001, ARG002
        session: Session,
        store,  # noqa: ANN001
    ) -> None:
        """§4.11: касса недоступна — `unknown` и причина, а не «камера пропала»."""
        http = login(make_client(timeout_transport()))
        camera = add_device(http, store.id)

        response = http.post(f"{STORES_URL}/{store.id}/devices/check")

        assert response.status_code == 200
        body = response.json()
        assert body["agent_ok"] is False
        assert "не ответила" in body["message"]
        session.expire_all()
        device = session.get(StoreDevice, camera["id"])
        assert device.last_status == "unknown"
        assert device.last_error == "касса не смогла опросить подсеть"

    def test_agent_rejects_token_502(
        self,
        make_client,  # noqa: ANN001
        admin,  # noqa: ANN001, ARG002
        store,  # noqa: ANN001
    ) -> None:
        http = login(make_client(status_transport(401)))
        add_device(http, store.id)

        response = http.post(f"{STORES_URL}/{store.id}/devices/check")

        assert response.status_code == 502
        assert "токен" in response.json()["detail"].lower()

    def test_no_active_devices_is_reported(
        self,
        make_client,  # noqa: ANN001
        admin,  # noqa: ANN001, ARG002
        store,  # noqa: ANN001
    ) -> None:
        agent = ScriptedAgent()
        http = login(make_client(agent.transport))
        device = add_device(http, store.id)
        http.delete(f"{STORES_URL}/{store.id}/devices/{device['id']}")

        response = http.post(f"{STORES_URL}/{store.id}/devices/check")

        assert response.status_code == 200
        assert response.json()["agent_ok"] is False
        assert "нет активных устройств" in response.json()["message"]
        assert agent.paths() == [], "кассу не тревожим, если опрашивать нечего"

    def test_unknown_store_is_404(self, auth_client: TestClient) -> None:
        assert auth_client.post(f"{STORES_URL}/999/devices/check").status_code == 404

