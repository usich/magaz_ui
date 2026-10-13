"""Общие фикстуры тестов сервера.

Ключевая идея: приложение собирается тем же кодом, что и в проде
(`create_app(settings)` + реальный `lifespan`), но на изолированной временной
БД и временных ключах секретов. Ни одна фикстура не пишет в `server/data`.

Инвариант I-11 проверяется косвенно: подмена настроек здесь не требует правок
бизнес-кода.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from server.config import Settings
from server.core.security import SecretBox, generate_agent_token
from server.db.base import Base
from server.db.models import AdminUser, Store
from server.db.session import DatabaseManager, create_db_engine
from server.main import create_app
from server.services import admin_service, settings_service

#: Реквизиты тестового администратора
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "magaz-test-password"

#: Токен и IP тестового магазина (совпадают с `stores.agent_token`/`ip_address`)
STORE_TOKEN = "agent-token-for-tests"
STORE_IP = "10.10.5.10"


@pytest.fixture()
def settings(tmp_path) -> Settings:  # noqa: ANN001 - pytest tmp_path
    """Настройки сервера, полностью изолированные во временном каталоге.

    Health Monitor и Диспетчер выключены: тесты не должны ходить в сеть за спиной
    сценария. Там, где фоновая работа проверяется, она запускается явно
    (`run_cycle`, `HealthMonitor.poll_once`, `CommandDispatcher.process_once`)
    с подменённым транспортом.
    """
    return Settings(
        database_url=f"sqlite:///{(tmp_path / 'magaz.db').as_posix()}",
        secrets_key_path=tmp_path / "secrets.key",
        cookie_secret_path=tmp_path / "cookie_secret.key",
        frontend_dist_dir=tmp_path / "dist",
        serve_spa=True,
        health_monitor_enabled=False,
        dispatcher_enabled=False,
        log_file=tmp_path / "logs" / "server.log",
        log_level="WARNING",
        _env_file=None,
    )


@pytest.fixture()
def engine(settings: Settings) -> Iterator[Engine]:
    """Схема БД по моделям §4 (в проде её создаёт Alembic)."""
    eng = create_db_engine(settings.database_url)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    """Прямая сессия БД для тестов сервисов и моделей."""
    with Session(engine, expire_on_commit=False) as db_session:
        yield db_session


@pytest.fixture()
def app(settings: Settings, engine: Engine) -> FastAPI:  # noqa: ARG001 - схема нужна до старта
    """Приложение FastAPI на временной БД."""
    return create_app(settings)


@pytest.fixture()
def client(app: FastAPI) -> Iterator[TestClient]:
    """HTTP-клиент с пройденным `lifespan` (настройки по умолчанию подсеиваются)."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def admin(session: Session) -> AdminUser:
    """Администратор с известным паролем."""
    admin_user = admin_service.create_admin(
        session, username=ADMIN_USERNAME, password=ADMIN_PASSWORD
    )
    session.commit()
    return admin_user


@pytest.fixture()
def admin_credentials() -> dict[str, str]:
    """Логин/пароль администратора для `POST /auth/login`."""
    return {"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}


@pytest.fixture()
def auth_client(client: TestClient, admin: AdminUser, admin_credentials: dict[str, str]) -> TestClient:  # noqa: ARG001
    """Клиент, вошедший в дашборд (cookie сохранена)."""
    response = client.post("/api/v1/admin/auth/login", json=admin_credentials)
    assert response.status_code == 200, response.text
    return client


@pytest.fixture()
def store(session: Session) -> Store:
    """Активный магазин с известным токеном агента и IP."""
    store_row = Store(
        name="Магазин №5",
        ip_address=STORE_IP,
        cluster_name="srv-1c",
        database_name="trade",
        version_1c="8.3.24.1548",
        user_name_1c="Администратор",
        agent_token=STORE_TOKEN,
    )
    session.add(store_row)
    session.commit()
    return store_row


@pytest.fixture()
def agent_headers() -> dict[str, str]:
    """Заголовки агента: токен + IP магазина (проверка источника, §8.1)."""
    return {"X-Agent-Token": STORE_TOKEN, "X-Forwarded-For": STORE_IP}


@pytest.fixture()
def fresh_token() -> str:
    """Новый токен агента (проверка кнопки «перевыпустить токен»)."""
    return generate_agent_token()


@pytest.fixture()
def default_settings_seeded(session: Session) -> None:
    """Настройки §4.9 в БД (для тестов сервиса настроек)."""
    settings_service.ensure_defaults(session)
    session.commit()


@pytest.fixture()
def secret_box() -> SecretBox:
    """Шифровальщик секретов на одноразовом ключе (пароль 1С, П-1)."""
    return SecretBox(key=Fernet.generate_key())


@pytest.fixture()
def db_manager(settings: Settings) -> Iterator[DatabaseManager]:
    """Менеджер БД приложения (тот же файл, что у тестового приложения)."""
    manager = DatabaseManager(settings.database_url)
    yield manager
    manager.dispose()


@pytest.fixture()
def make_client(settings: Settings, engine: Engine) -> Iterator[Callable[..., TestClient]]:  # noqa: ARG001
    """Фабрика TestClient с подменённым транспортом к агентам (Э2).

    Нужна там, где сервер сам обращается к кассе (`POST /stores/{id}/check`,
    Health Monitor): транспорт `httpx.MockTransport` задаётся **до** старта
    приложения, поэтому попадает в контекст через `app.state.agent_transport`.
    """
    created: list[TestClient] = []

    def factory(transport: httpx.AsyncBaseTransport | None = None) -> TestClient:
        application = create_app(settings)
        if transport is not None:
            application.state.agent_transport = transport
        test_client = TestClient(application)
        test_client.__enter__()
        created.append(test_client)
        return test_client

    yield factory
    for test_client in created:
        test_client.__exit__(None, None, None)
