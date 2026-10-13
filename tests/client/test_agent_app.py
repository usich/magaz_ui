"""Тесты HTTP-эндпоинтов агента (§5.1.2, §8.1) и его конфига (§7.2).

Агент проверяется как обычное FastAPI-приложение через TestClient: реальная
касса, 1С и `msg.exe` для этих проверок не нужны (§13).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from client.agent.app import create_agent_app
from client.agent.config import (
    AGENT_CONFIG_FILE_NAME,
    default_config_path,
    describe_os,
    get_application_root,
    load_config,
)
from shared.timeutil import utcnow
from shared.version import APP_VERSION
from tests.client.conftest import AGENT_TOKEN, CONFIG_PATH, STORE_ID, make_config

HEALTH_URL = "/health"
VERSION_URL = "/version"


class TestLiveness:
    """`GET /` — перенос v2 1:1: отвечает без токена (§5.1.1)."""

    def test_root_is_open(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {"status": "online"}

    def test_root_ignores_token(self, client: TestClient) -> None:
        assert client.get("/", headers={"X-Agent-Token": "stolen-token"}).status_code == 200


class TestAgentAuth:
    """FR-11, §8.1: `GET /health` и `/version` — только с токеном магазина."""

    def test_health_without_token_is_401(self, client: TestClient) -> None:
        assert client.get(HEALTH_URL).status_code == 401

    def test_health_with_wrong_token_is_401(self, client: TestClient) -> None:
        response = client.get(HEALTH_URL, headers={"X-Agent-Token": "stolen-token"})
        assert response.status_code == 401
        assert "X-Agent-Token" in response.text

    def test_health_with_missing_config_token_is_503(self) -> None:
        app = create_agent_app(make_config(agent_token=""))
        with TestClient(app) as client:
            assert client.get(HEALTH_URL, headers={"X-Agent-Token": "any"}).status_code == 503

    def test_allowed_server_ips_accepts_known_source(self) -> None:
        app = create_agent_app(make_config(allowed_server_ips=["testclient"]))
        with TestClient(app) as client:
            assert client.get(HEALTH_URL, headers={"X-Agent-Token": AGENT_TOKEN}).status_code == 200

    def test_foreign_source_ip_is_403(self) -> None:
        app = create_agent_app(make_config(allowed_server_ips=["10.10.0.1"]))
        with TestClient(app) as client:
            response = client.get(HEALTH_URL, headers={"X-Agent-Token": AGENT_TOKEN})
        assert response.status_code == 403
        assert "адреса сервера" in response.text


class TestHealthPayload:
    """`GET /health` — данные, на которых держится мониторинг (§5.1.2)."""

    def test_reports_identity_and_versions(
        self, client: TestClient, agent_headers: dict[str, str]
    ) -> None:
        response = client.get(HEALTH_URL, headers=agent_headers)
        body = response.json()

        assert response.status_code == 200
        assert body["status"] == "online"
        assert body["store_id"] == STORE_ID
        assert body["agent_version"] == APP_VERSION
        assert body["uptime_sec"] >= 0
        assert body["work_dir"] == str(get_application_root())
        assert body["base_1c_path"] == "C:/Program Files (x86)/1cv8"
        assert body["os"] == describe_os()
        assert body["busy"] == {"merge": False, "update": False}
        assert body["sessions"] == []
        assert body["credentials_version"] == 3, "П-1: версия профиля 1С"
        assert body["devices_version"] == 4, "Д-2: версия списка оборудования"

    def test_uptime_counts_from_started_at(self) -> None:
        app = create_agent_app(make_config(), started_at=utcnow() - timedelta(minutes=5))
        with TestClient(app) as client:
            body = client.get(HEALTH_URL, headers={"X-Agent-Token": AGENT_TOKEN}).json()
        assert body["uptime_sec"] >= 300

    def test_config_base_1c_path_is_used(self) -> None:
        app = create_agent_app(make_config(base_1c_path="D:/1cv8"))
        with TestClient(app) as client:
            body = client.get(HEALTH_URL, headers={"X-Agent-Token": AGENT_TOKEN}).json()
        assert body["base_1c_path"] == "D:/1cv8", "I-14: путь к платформе — из конфига"


class TestVersionEndpoint:
    """`GET /version` — версия агента и рабочие пути (§5.1.2)."""

    def test_returns_working_paths(
        self, client: TestClient, agent_headers: dict[str, str]
    ) -> None:
        body = client.get(VERSION_URL, headers=agent_headers).json()

        assert body["store_id"] == STORE_ID
        assert body["store_name"] == "Магазин №5"
        assert body["agent_version"] == APP_VERSION
        assert body["port"] == 8383
        assert body["work_dir"] == str(get_application_root())
        assert body["default_file_name"] == "update"
        assert body["config_path"] == str(CONFIG_PATH)
        assert body["credentials_version"] == 3
        assert body["devices_version"] == 4

    def test_requires_token(self, client: TestClient) -> None:
        assert client.get(VERSION_URL).status_code == 401


class TestAgentConfigFile:
    """§7.2: чтение `agent-config.json` и работа с путями."""

    def test_loads_values_from_file(self, config_file: Path) -> None:
        config = load_config(config_file)

        assert config.store_id == STORE_ID
        assert config.agent_token == AGENT_TOKEN
        assert config.base_1c_path == "D:/1cv8"
        assert config.credentials_version == 5
        assert config.devices_version == 2
        assert config.allowed_server_ips == [], "по умолчанию проверка источника выключена"

    def test_missing_file_is_clear_error(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Не найден конфиг агента"):
            load_config(tmp_path / "нет-такого.json")

    def test_unknown_fields_are_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / AGENT_CONFIG_FILE_NAME
        path.write_text('{"store_id": 1, "unknown_future_field": true}', encoding="utf-8")
        assert load_config(path).store_id == 1, "конфиг переносим между версиями агента"

    def test_default_config_path_uses_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MAGAZ_AGENT_CONFIG", str(tmp_path / "custom.json"))
        assert default_config_path() == tmp_path / "custom.json"

    def test_work_dir_from_config_overrides_root(self) -> None:
        config = make_config(work_dir=r"D:\UpdateFiles")
        assert str(config.work_path) == r"D:\UpdateFiles"

    def test_describe_os_is_not_empty(self) -> None:
        assert describe_os().strip()