"""Пул соединений БД: размеры, диагностика и `503` вместо 30-секундного зависания (Д-30).

Почему это отдельный файл: раньше ошибка `QueuePool limit of size 5 overflow 10
reached` проявлялась только на боевом стенде — в тестах пул никогда не
исчерпывался, потому что каждый тест ходит в БД по одному запросу. Здесь пул
исчерпывается намеренно (`pool_size=1`, `max_overflow=0`), чтобы проверить
поведение сервера именно в том состоянии, в котором он «вставал».
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from server.core.context import AppContext
from server.core.dependencies import POOL_EXHAUSTED_DETAIL, get_db
from server.core.security import SessionSigner
from server.db.base import Base
from server.db.session import (
    DEFAULT_MAX_OVERFLOW,
    DEFAULT_POOL_SIZE,
    DEFAULT_POOL_TIMEOUT_SEC,
    DatabaseManager,
    create_db_engine,
    pool_status,
)
from tests.server.agent_mock import health_body, transport_from_handler
from tests.server.conftest import ADMIN_PASSWORD, ADMIN_USERNAME

#: Диагностика сервера, где виден пул (Д-30)
SYSTEM_INFO_URL = "/api/v1/admin/system/info"
#: Вход в дашборд и кнопка «Проверить сейчас» (§5.2.2, §11.2)
LOGIN_URL = "/api/v1/admin/auth/login"
STORES_URL = "/api/v1/admin/stores"


@pytest.fixture()
def probe_engine(tmp_path) -> Iterator[Engine]:  # noqa: ANN001 - pytest tmp_path
    """Файловая БД с маленьким, но управляемым пулом (7 + 3)."""
    engine = create_db_engine(
        f"sqlite:///{(tmp_path / 'pool.db').as_posix()}",
        pool_size=7,
        max_overflow=3,
        pool_timeout=5.0,
    )
    yield engine
    engine.dispose()


class TestPoolStatus:
    """`pool_status()` — то, что видит администратор в `/system/info`."""

    def test_reports_queue_pool_capacity_and_occupancy(self, probe_engine: Engine) -> None:
        assert pool_status(probe_engine) == {
            "class": "QueuePool",
            "size": 7,
            "max_overflow": 3,
            "checked_out": 0,
            "available": 10,
            "timeout_sec": 5.0,
        }

        with probe_engine.connect():
            held = pool_status(probe_engine)

        assert held["checked_out"] == 1, "взятое соединение видно в диагностике"
        assert held["available"] == 9, "свободных слотов на одно меньше"
        assert pool_status(probe_engine)["checked_out"] == 0, "после закрытия — снова свободно"

    def test_reports_defaults_from_database_manager(self, settings) -> None:  # noqa: ANN001
        """`DatabaseManager` без явных аргументов берёт настройки Д-30, а не умолчания SQLAlchemy."""
        manager = DatabaseManager(settings.database_url)
        try:
            status = manager.pool_status()
            assert status["size"] == DEFAULT_POOL_SIZE
            assert status["max_overflow"] == DEFAULT_MAX_OVERFLOW
            assert status["timeout_sec"] == DEFAULT_POOL_TIMEOUT_SEC
        finally:
            manager.dispose()

    def test_in_memory_db_reports_pool_without_queue(self) -> None:
        """In-memory SQLite живёт на `SingletonThreadPool` — полей очереди у него нет."""
        engine = create_db_engine(
            "sqlite:///:memory:", pool_size=7, max_overflow=3, pool_timeout=5.0
        )
        try:
            status = pool_status(engine)
        finally:
            engine.dispose()

        assert status["class"] == "SingletonThreadPool"
        assert "max_overflow" not in status
        assert "available" not in status, "у пула без очереди счётчика свободных слотов нет"

    def test_pool_without_size_does_not_break_diagnostics(self) -> None:
        """`NullPool` не имеет ни размера, ни таймаута — диагностика не должна падать."""
        engine = create_engine("sqlite://", poolclass=NullPool)
        try:
            assert pool_status(engine) == {"class": "NullPool"}
        finally:
            engine.dispose()


class TestPoolExhaustion:
    """Исчерпанный пул — это `503` с понятным текстом, а не зависание на 30 с."""

    @pytest.fixture()
    def tiny_context(self, settings, secret_box) -> Iterator[AppContext]:  # noqa: ANN001
        """Контекст приложения с пулом на одно соединение и коротким ожиданием."""
        manager = DatabaseManager(
            settings.database_url,
            pool_size=1,
            max_overflow=0,
            pool_timeout=0.05,
        )
        Base.metadata.create_all(manager.engine)
        context = AppContext(
            settings=settings,
            db=manager,
            secret_box=secret_box,
            session_signer=SessionSigner(Fernet.generate_key()),
        )
        yield context
        manager.dispose()

    def test_get_db_returns_503_when_no_connection_left(self, tiny_context: AppContext) -> None:
        generator = get_db(tiny_context)
        with tiny_context.db.engine.connect():  # единственное соединение занято
            assert tiny_context.db.pool_status()["checked_out"] == 1
            with pytest.raises(HTTPException) as excinfo:
                next(generator)
        generator.close()

        error = excinfo.value
        assert error.status_code == 503
        assert error.detail == POOL_EXHAUSTED_DETAIL, "администратор читает причину, а не трассировку"
        assert error.headers == {"Retry-After": "5"}, "SPA повторяет запрос по заголовку"

    def test_get_db_yields_working_session_when_pool_is_free(self, tiny_context: AppContext) -> None:
        """Обратная сторона: свободный пул — обычная сессия, соединение берётся заранее."""
        generator = get_db(tiny_context)
        session = next(generator)
        try:
            assert tiny_context.db.pool_status()["checked_out"] == 1, "checkout до тела обработчика"
            assert session.execute(text("SELECT 1")).scalar() == 1
        finally:
            generator.close()  # коммит и close делает сам генератор

        assert tiny_context.db.pool_status()["checked_out"] == 0, "соединение вернулось в пул"


class TestSystemInfoPoolDiagnostics:
    """`/system/info` отдаёт пул — по нему видно приближение к исчерпанию."""

    def test_system_info_includes_pool_state(self, auth_client: TestClient, settings) -> None:  # noqa: ANN001
        response = auth_client.get(SYSTEM_INFO_URL)

        assert response.status_code == 200, response.text
        pool = response.json()["database"]["pool"]
        assert pool["class"] == "QueuePool"
        assert pool["size"] == settings.db_pool_size
        assert pool["max_overflow"] == settings.db_max_overflow
        assert pool["timeout_sec"] == settings.db_pool_timeout_sec
        assert pool["checked_out"] >= 1, "запрос сам держит соединение, пока считает диагностику"
        assert pool["available"] == pool["size"] + pool["max_overflow"] - pool["checked_out"]


class TestNoConnectionHeldDuringAgentCall:
    """«Проверить сейчас» ждёт кассу, не занимая соединений пула (Д-30).

    Проверка идёт ровно по инварианту I-17: в момент сетевого ожидания агента в
    пуле не должно быть ни одного занятого соединения — иначе несколько
    параллельных проверок офлайн-касс вычерпывают пул и вместе с ними встаёт
    диспетчер.
    """

    def test_store_check_frees_pool_before_network_wait(
        self, make_client, admin, store  # noqa: ANN001, ARG002
    ) -> None:
        holder: dict[str, TestClient] = {}
        observed: list[dict[str, int]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            # Транспорт вызывается в момент, когда сервер ждёт ответ кассы
            observed.append(holder["client"].app.state.ctx.db.pool_status())
            if request.url.path == "/version":
                return httpx.Response(200, json={"agent_version": "1.0.0"})
            return httpx.Response(200, json=health_body(store_id=store.id))

        client = make_client(transport_from_handler(handler))
        holder["client"] = client
        login = client.post(
            LOGIN_URL, json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
        )
        assert login.status_code == 200, login.text

        response = client.post(f"{STORES_URL}/{store.id}/check")

        assert response.status_code == 200, response.text
        assert observed, "сервер действительно опросил кассу"
        assert observed[0]["checked_out"] == 0, (
            "на время ожидания агента пул свободен: сессии входа и чтения карточки закрыты"
        )
        assert client.app.state.ctx.db.pool_status()["checked_out"] == 0, (
            "запись результата тоже не оставляет соединение занятым"
        )

