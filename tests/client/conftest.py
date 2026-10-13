"""Тесты агента кассы (`client/`) — состав Э2."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from client.agent.app import create_agent_app
from client.agent.config import AgentConfig

#: Токен и id магазина, которые попадают в `agent-config.json` (§7.2)
AGENT_TOKEN = "agent-token-for-tests"
STORE_ID = 12

#: Путь конфига для тестов (в UI он показывается как подсказка, §11.2)
CONFIG_PATH = Path(r"C:\MagazAgent\agent-config.json")


def make_config(**overrides) -> AgentConfig:  # noqa: ANN003
    """Конфиг агента с разумными значениями по умолчанию (§7.2)."""
    data: dict = {
        "store_id": STORE_ID,
        "store_name": "Магазин №5",
        "server_url": "http://10.10.0.1:8080",
        "agent_token": AGENT_TOKEN,
        "port": 8383,
        "one_c_profile": {
            "user_name": "Администратор",
            "password_enc": None,
            "credentials_version": 3,
        },
        "devices_version": 4,
    }
    data.update(overrides)
    return AgentConfig.model_validate(data)


@pytest.fixture()
def config() -> AgentConfig:
    """Конфиг агента, как его вписывают в `agent-config.json` (§7.2)."""
    return make_config()


@pytest.fixture()
def agent_app(config: AgentConfig) -> FastAPI:
    """Приложение агента без сети: TestClient обращается к нему напрямую."""
    return create_agent_app(config, config_path=CONFIG_PATH)


@pytest.fixture()
def client(agent_app: FastAPI) -> Iterator[TestClient]:
    """HTTP-клиент агента."""
    with TestClient(agent_app) as test_client:
        yield test_client


@pytest.fixture()
def agent_headers() -> dict[str, str]:
    """Заголовки сервера: токен магазина (§5.1)."""
    return {"X-Agent-Token": AGENT_TOKEN}


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    """Реальный `agent-config.json` на диске (проверка `load_config`)."""
    path = tmp_path / "agent-config.json"
    path.write_text(
        json.dumps(
            {
                "store_id": STORE_ID,
                "store_name": "Магазин №5",
                "server_url": "http://10.10.0.1:8080",
                "agent_token": AGENT_TOKEN,
                "port": 8383,
                "base_1c_path": "D:/1cv8",
                "one_c_profile": {"user_name": "Администратор", "credentials_version": 5},
                "devices_version": 2,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path
