"""Тесты админского API (§5.2.2) для состава Э1: вход, настройки, аудит, диагностика."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from server.db.models import AuditLog, Setting
from tests.server.conftest import ADMIN_PASSWORD, ADMIN_USERNAME

LOGIN_URL = "/api/v1/admin/auth/login"
LOGOUT_URL = "/api/v1/admin/auth/logout"
ME_URL = "/api/v1/admin/auth/me"
SETTINGS_URL = "/api/v1/admin/settings"
AUDIT_URL = "/api/v1/admin/audit"
SYSTEM_INFO_URL = "/api/v1/admin/system/info"
STORES_URL = "/api/v1/admin/stores"
HEALTH_URL = "/api/v1/health"


def audit_actions(engine: Engine) -> list[str]:
    """Действия из журнала аудита, от свежих к старым (свежая сессия — свежий снимок)."""
    with Session(engine) as check:
        rows = check.scalars(select(AuditLog).order_by(AuditLog.id.desc())).all()
        return [row.action for row in rows]


class TestPublicHealth:
    """Публичная проверка живости: нужна до входа и при развёртывании."""

    def test_health_is_public(self, client: TestClient) -> None:
        response = client.get(HEALTH_URL)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "online"
        assert body["app_version"]


class TestLogin:
    """§4.2, §8.1: вход по логину/паролю с подписанной cookie."""

    def test_successful_login_sets_cookie(
        self, client: TestClient, admin, admin_credentials: dict[str, str], engine: Engine  # noqa: ANN001, ARG002
    ) -> None:
        response = client.post(LOGIN_URL, json=admin_credentials)

        assert response.status_code == 200
        assert response.json()["username"] == ADMIN_USERNAME
        assert "password" not in response.text.lower()
        assert client.cookies.get("magaz_admin"), "сессионная cookie выдана"
        assert audit_actions(engine)[0] == "admin_login"

    def test_wrong_password_is_401_and_audited(
        self, client: TestClient, admin, engine: Engine  # noqa: ANN001, ARG002
    ) -> None:
        response = client.post(
            LOGIN_URL, json={"username": ADMIN_USERNAME, "password": "wrong-password"}
        )

        assert response.status_code == 401
        assert not client.cookies.get("magaz_admin")
        assert audit_actions(engine)[0] == "admin_login_failed"

    def test_unknown_user_is_401(self, client: TestClient, admin) -> None:  # noqa: ANN001, ARG002
        response = client.post(
            LOGIN_URL, json={"username": "nobody", "password": ADMIN_PASSWORD}
        )
        assert response.status_code == 401

    def test_me_requires_authentication(self, client: TestClient) -> None:
        assert client.get(ME_URL).status_code == 401

    def test_me_returns_current_admin(
        self, auth_client: TestClient, admin  # noqa: ANN001, ARG002
    ) -> None:
        response = auth_client.get(ME_URL)
        body = response.json()

        assert response.status_code == 200
        assert body["id"] == admin.id
        assert body["username"] == ADMIN_USERNAME
        assert body["is_active"] is True
        assert body["last_login_at"], "время последнего входа обновилось при логине"

    def test_logout_drops_session(
        self, auth_client: TestClient, admin, engine: Engine  # noqa: ANN001, ARG002
    ) -> None:
        assert auth_client.post(LOGOUT_URL).status_code == 200
        assert auth_client.get(ME_URL).status_code == 401
        assert "admin_logout" in audit_actions(engine)

    def test_tampered_cookie_is_rejected(
        self, auth_client: TestClient, admin  # noqa: ANN001, ARG002
    ) -> None:
        auth_client.cookies.set("magaz_admin", "definitely-not-a-signed-token")
        assert auth_client.get(ME_URL).status_code == 401


class TestSettingsApi:
    """§4.9: настройки читаются и меняются из UI; каждое изменение — в аудите."""

    def test_settings_require_authentication(self, client: TestClient) -> None:
        assert client.get(SETTINGS_URL).status_code == 401

    def test_list_returns_all_spec_keys_with_defaults(
        self, auth_client: TestClient, admin  # noqa: ANN001, ARG002
    ) -> None:
        response = auth_client.get(SETTINGS_URL)
        body = response.json()

        assert response.status_code == 200
        by_key = {item["key"]: item for item in body}
        assert len(by_key) == 20, "все ключи §4.9 присутствуют (включая добавки Д-7/Д-21/Д-24/Д-25)"
        assert by_key["health_interval_sec"]["value"] == "15"
        assert by_key["health_interval_sec"]["default"] == "15"
        assert by_key["health_interval_sec"]["is_default"] is True
        assert by_key["health_interval_sec"]["description"]
        assert by_key["default_file_name"]["value"] == "update"
        assert by_key["local_update_interval_sec"]["value"] == "5"  # Д-24
        assert by_key["local_update_concurrency"]["value"] == "5"  # Д-25

    def test_update_changes_value_and_writes_audit(
        self, auth_client: TestClient, admin, engine: Engine  # noqa: ANN001, ARG002
    ) -> None:
        response = auth_client.put(
            SETTINGS_URL, json={"values": {"health_interval_sec": "30"}}
        )

        assert response.status_code == 200
        by_key = {item["key"]: item for item in response.json()}
        assert by_key["health_interval_sec"]["value"] == "30"
        assert by_key["health_interval_sec"]["is_default"] is False

        with Session(engine) as check:
            assert check.get(Setting, "health_interval_sec").value == "30"
        assert audit_actions(engine)[0] == "settings_update"

    def test_invalid_value_is_422(self, auth_client: TestClient, admin) -> None:  # noqa: ANN001, ARG002
        response = auth_client.put(
            SETTINGS_URL, json={"values": {"health_interval_sec": "много"}}
        )
        assert response.status_code == 422
        assert "целым числом" in response.text

    def test_unknown_key_is_422(self, auth_client: TestClient, admin) -> None:  # noqa: ANN001, ARG002
        response = auth_client.put(SETTINGS_URL, json={"values": {"hack_the_planet": "1"}})
        assert response.status_code == 422

    def test_empty_payload_is_422(self, auth_client: TestClient, admin) -> None:  # noqa: ANN001, ARG002
        assert auth_client.put(SETTINGS_URL, json={"values": {}}).status_code == 422


class TestAuditApi:
    """§4.10: журнал аудита доступен только администратору."""

    def test_audit_requires_authentication(self, client: TestClient) -> None:
        assert client.get(AUDIT_URL).status_code == 401

    def test_login_and_settings_changes_are_visible(self, auth_client: TestClient, admin) -> None:  # noqa: ANN001, ARG002
        auth_client.put(SETTINGS_URL, json={"values": {"check_poll_interval_sec": "7"}})
        response = auth_client.get(AUDIT_URL, params={"limit": 10})

        assert response.status_code == 200
        actions = [item["action"] for item in response.json()]
        assert "admin_login" in actions
        assert "settings_update" in actions
        entry = response.json()[0]
        assert entry["actor"] == ADMIN_USERNAME
        assert entry["created_at"], "время в ISO-8601"

    def test_no_password_in_audit_details(self, auth_client: TestClient, admin) -> None:  # noqa: ANN001, ARG002
        response = auth_client.get(AUDIT_URL, params={"limit": 50})
        assert ADMIN_PASSWORD not in response.text


class TestSystemInfo:
    """Диагностика сервера (состав Э1)."""

    def test_requires_authentication(self, client: TestClient) -> None:
        assert client.get(SYSTEM_INFO_URL).status_code == 401

    def test_reports_version_database_and_spa_state(
        self, auth_client: TestClient, admin, store  # noqa: ANN001, ARG002
    ) -> None:
        response = auth_client.get(SYSTEM_INFO_URL)
        body = response.json()

        assert response.status_code == 200
        assert body["app_name"] == "Magaz UI"
        assert body["app_version"]
        assert body["database"]["kind"] == "sqlite"
        assert body["spa"]["built"] is False, "SPA ещё не собрана в тестах"
        assert body["counts"]["stores_total"] == 1
        assert body["counts"]["stores_active"] == 1, "Д-14: плитка главной считает активные"
        assert body["counts"]["stores_inactive"] == 0
        assert body["counts"]["admins_total"] == 1
        assert body["counts"]["jobs_active"] == 0, "Э4: плитка «Задач в работе»"
        assert body["settings_keys"] == 20  # §4.9, включая Д-7/Д-21/Д-24/Д-25


class TestSystemCountersForDisabledStores:
    """Д-14: отключённый магазин не считается магазином «в работе».

    «Отключить» — мягкое удаление (§5.2.2): строка остаётся в таблице ради
    истории, отчётов и повторного включения. Поэтому сервер отдаёт оба числа:
    `stores_total` (строки) и `stores_active` (в работе). Плитка главной берёт
    второе — иначе после отключения магазина на главной оставалось «2».
    """

    def test_disabled_store_is_counted_separately(self, auth_client: TestClient, store) -> None:  # noqa: ANN001, ARG002
        created = auth_client.post(
            STORES_URL,
            json={
                "name": "Магазин №14",
                "ip_address": "10.10.14.14",
                "cluster_name": "srv-1c",
                "database_name": "trade",
                "version_1c": "8.3.24.1548",
                "user_name_1c": "Администратор",
            },
        )
        assert created.status_code == 200, created.text
        store_id = created.json()["id"]

        disabled = auth_client.delete(f"{STORES_URL}/{store_id}")
        assert disabled.status_code == 200, disabled.text
        counts = auth_client.get(SYSTEM_INFO_URL).json()["counts"]

        assert counts["stores_total"] == 2, "строка осталась в таблице: история и отчёты"
        assert counts["stores_active"] == 1, "плитка главной показывает рабочее число"
        assert counts["stores_inactive"] == 1, "видно, куда делся магазин"
        assert counts["stores_online"] + counts["stores_offline"] == 1, (
            "«онлайн» + «офлайн» — это тоже только магазины в работе"
        )

