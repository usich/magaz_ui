"""Тесты единой точки исходящих вызовов к агентам (§6, §8.1, I-11).

Сеть либо подменяется `httpx.MockTransport`, либо поднимается крошечный локальный
HTTP-сервер: так проверяется и поведение на реальном сокете (проверка «прокси не
вмешивается»), и разбор ошибок агента.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from server.config import Settings
from server.core.client_http import (
    AgentAuthError,
    AgentClient,
    AgentOfflineError,
    AgentProtocolError,
    AgentTarget,
    build_agent_target,
)
from server.db.models import Store
from tests.server.agent_mock import health_body, status_transport

#: Заведомо недоступный прокси: если httpx его послушает, запрос не дойдёт
BROKEN_PROXY = "http://127.0.0.1:9"


async def start_stub_agent(body: dict[str, object]) -> AsyncIterator[tuple[str, int]]:
    """Поднять локальный «агент»: один ответ `200` с телом `/health`."""
    payload = json.dumps(body).encode("utf-8")

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(4096)
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + payload
            )
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, OSError):  # pragma: no cover - зависит от ОС
                pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    try:
        yield "127.0.0.1", port
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture()
async def stub_agent() -> AsyncIterator[tuple[str, int]]:
    """Локальный «агент» с корректным ответом `/health`."""
    async for endpoint in start_stub_agent(health_body(agent_version="9.9.9")):
        yield endpoint


class TestAgentTarget:
    """Адрес агента собирается из `stores` и настроек (I-11)."""

    def test_uses_store_ip_and_port(self, store: Store, settings: Settings) -> None:
        target = build_agent_target(store, settings)

        assert target.base_url == f"http://{store.ip_address}:{store.agent_port}"
        assert target.token == store.agent_token
        assert target.headers["X-Agent-Token"] == store.agent_token

    def test_default_port_is_used_when_empty(self, store: Store, settings: Settings) -> None:
        store.agent_port = 0
        assert build_agent_target(store, settings).base_url.endswith(":8383")


class TestProxyIsIgnored:
    """Регресс Э2: прокси из окружения/настроек ОС не перехватывает кассу."""

    async def test_request_goes_directly_to_agent(
        self, stub_agent: tuple[str, int], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        host, port = stub_agent
        monkeypatch.setenv("HTTP_PROXY", BROKEN_PROXY)
        monkeypatch.setenv("HTTPS_PROXY", BROKEN_PROXY)
        monkeypatch.setenv("ALL_PROXY", BROKEN_PROXY)

        target = AgentTarget(store_id=1, base_url=f"http://{host}:{port}", token="token")
        async with AgentClient(target, timeout=3.0) as client:
            health = await client.get_health()

        assert health.agent_version == "9.9.9", "ответ пришёл от агента, а не от прокси"


class TestTypedErrors:
    """Ошибки обращения к агенту различаются по типу (§5.2.2, Приложение B)."""

    async def test_auth_error(self) -> None:
        target = AgentTarget(store_id=1, base_url="http://agent", token="t")
        async with AgentClient(target, transport=status_transport(401)) as client:
            with pytest.raises(AgentAuthError) as error:
                await client.get_health()
        assert error.value.status_code == 401

    async def test_protocol_error_on_server_error(self) -> None:
        target = AgentTarget(store_id=1, base_url="http://agent", token="t")
        async with AgentClient(target, transport=status_transport(500)) as client:
            with pytest.raises(AgentProtocolError):
                await client.get_health()

    async def test_protocol_error_on_broken_payload(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"не": "то"}))
        target = AgentTarget(store_id=1, base_url="http://agent", token="t")
        async with AgentClient(target, transport=transport) as client:
            with pytest.raises(AgentProtocolError, match="неожиданный /health"):
                await client.get_health()

    async def test_offline_error_on_connection_failure(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("нет соединения", request=request)

        target = AgentTarget(store_id=1, base_url="http://agent", token="t")
        async with AgentClient(target, transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(AgentOfflineError):
                await client.get_version()

    async def test_version_returns_dict(self) -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"agent_version": "1.0.0"})
        )
        target = AgentTarget(store_id=1, base_url="http://agent", token="t")
        async with AgentClient(target, transport=transport) as client:
            assert (await client.get_version())["agent_version"] == "1.0.0"
