"""Тесты агента кассы (`client/`) — состав Э2–Э3.

Касса проверяется как обычное FastAPI-приложение: реальная 1С, `msg.exe` и
`taskkill` не запускаются никогда (§7.5, §13) — внешние процессы подменяются
`RecordingProvider`, а рабочая папка 1С — временной (fixture `work_dir`).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from client.agent.app import create_agent_app
from client.agent.config import DEFAULT_BASE_1C_PATH, AgentConfig
from client.agent.one_c import OneCPaths

#: Токен и id магазина, которые попадают в `agent-config.json` (§7.2)
AGENT_TOKEN = "agent-token-for-tests"
STORE_ID = 12

#: Путь конфига для тестов (в UI он показывается как подсказка, §11.2)
CONFIG_PATH = Path(r"C:\MagazAgent\agent-config.json")

#: Что «пишет в консоль» подменённая 1С: проверяем I-6 (вывод попадает в лог)
RECORDED_OUTPUT = "вывод 1cv8.exe\n"


class RecordingProvider:
    """Провайдер процессов для тестов (§7.5): запоминает argv и «играет» 1С.

    Реальные `1cv8.exe`, `msg.exe` и `taskkill` не запускаются. Если операция
    должна «идти долго» (`blocked=True`), провайдер ждёт `release()` — на этом
    строится проверка мьютекса и `409 Conflict` (I-13).
    """

    def __init__(
        self,
        *,
        blocked: bool = False,
        success_message: str | None = None,
        quiet_available: bool = True,
    ) -> None:
        self.commands: list[list[str]] = []
        self.quiet_commands: list[list[str]] = []
        self.success_message = success_message
        #: Доступны ли утилиты Windows (`msg`, `taskkill`) — «урезанная» Windows
        self.quiet_available = quiet_available
        self._release = asyncio.Event()
        if not blocked:
            self._release.set()

    def release(self) -> None:
        """Разрешить «зависшей» операции завершиться."""
        self._release.set()

    async def run_captured(
        self, args: list[str], *, log_path: Path | None = None, timeout: float | None = None
    ) -> int | None:
        """Запомнить команду и при необходимости записать в лог сообщение об успехе."""
        self.commands.append(list(args))
        if not self._release.is_set():
            await self._release.wait()

        out_path = self._log_path_from(args)
        if self.success_message and out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(self.success_message, encoding="utf-8")
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(RECORDED_OUTPUT)
        return 0

    async def run_quiet(self, args: list[str]) -> bool:
        """Запомнить вызов утилиты Windows (`msg`, `taskkill`) и «выполнить» её."""
        self.quiet_commands.append(list(args))
        return self.quiet_available

    @staticmethod
    def _log_path_from(args: list[str]) -> Path | None:
        """Путь лога 1С из ключа `/Out` — куда «пишет» подменённая платформа."""
        if "/Out" not in args:
            return None
        return Path(args[args.index("/Out") + 1])



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


# --- Э3: рабочая папка 1С, пути и подменённая платформа (§7.5, §13) ---------


@pytest.fixture()
def work_dir(tmp_path: Path) -> Path:
    """Рабочая папка агента: `.cf/.cfe/.xml`, логи 1С и `agent-state.json` (§7.1)."""
    directory = tmp_path / "MagazAgent"
    directory.mkdir()
    return directory


@pytest.fixture()
def one_c_config(work_dir: Path) -> AgentConfig:
    """Конфиг агента с временной рабочей папкой: файлы 1С не трогаем (§7.2)."""
    return make_config(work_dir=str(work_dir))


@pytest.fixture()
def one_c_paths(work_dir: Path) -> OneCPaths:
    """Пути 1С для проверок команд: путь к платформе берётся из конфига (I-14)."""
    return OneCPaths(work_dir=work_dir, base_1c_path=Path(DEFAULT_BASE_1C_PATH))


@pytest.fixture()
def fake_provider() -> RecordingProvider:
    """Подменённый провайдер процессов: argv запоминается, 1С не запускается."""
    return RecordingProvider()


@pytest.fixture()
def one_c_app(one_c_config: AgentConfig, fake_provider: RecordingProvider) -> FastAPI:
    """Приложение агента с временной рабочей папкой и подменённой 1С (§7.5)."""
    return create_agent_app(one_c_config, provider=fake_provider)


@pytest.fixture()
def one_c_client(one_c_app: FastAPI) -> Iterator[TestClient]:
    """Синхронный клиент агента: проверки авторизации и мгновенных ответов."""
    with TestClient(one_c_app) as test_client:
        yield test_client


@pytest.fixture()
async def agent_http(one_c_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Асинхронный клиент к агенту: позволяет дождаться фоновой операции.

    Пример: `await one_c_app.state.agent_runner.wait_idle()` — и только потом
    проверяем `agent-state.json` и статус `/check-merge`.
    """
    transport = httpx.ASGITransport(app=one_c_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
        yield client
