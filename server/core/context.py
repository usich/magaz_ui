"""Контейнер ресурсов приложения (`app.state.ctx`).

Инвариант I-10: импорт модулей не создаёт побочных эффектов. Все «тяжёлые»
ресурсы (engine, ключи шифрования, подпись cookie) собираются один раз в
`lifespan` и живут в контексте — из него их берут зависимости FastAPI.
Тесты так же собирают свой контекст с временной БД, поэтому глобального
состояния в коде нет.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from server.config import Settings, get_settings
from server.core.security import COOKIE_SALT, SecretBox, SessionSigner, load_or_create_secret
from server.db.session import DatabaseManager

__all__ = ["AppContext", "build_app_context"]


@dataclass(slots=True)
class AppContext:
    """Ресурсы, общие для всех запросов сервера."""

    settings: Settings
    db: DatabaseManager
    secret_box: SecretBox
    session_signer: SessionSigner
    #: Транспорт httpx для исходящих вызовов к агентам (I-11): `None` — реальная
    #: сеть, в тестах подменяется `httpx.MockTransport`
    agent_transport: httpx.AsyncBaseTransport | None = None

    @property
    def frontend_dist(self) -> Path:
        """Каталог собранной SPA (`frontend\\dist`, §5.2.1)."""
        return Path(self.settings.frontend_dist_dir)

    def dispose(self) -> None:
        """Закрыть пул соединений при остановке приложения."""
        self.db.dispose()


def build_app_context(
    settings: Settings | None = None,
    *,
    agent_transport: httpx.AsyncBaseTransport | None = None,
) -> AppContext:
    """Собрать контекст приложения (вызывается из `lifespan` и из тестов)."""
    resolved = settings or get_settings()
    return AppContext(
        settings=resolved,
        db=DatabaseManager(resolved.database_url, echo=resolved.sql_echo),
        secret_box=SecretBox.from_path(resolved.secrets_key_path, resolved.secret_key or None),
        session_signer=SessionSigner(
            load_or_create_secret(resolved.cookie_secret_path, resolved.cookie_secret or None),
            salt=COOKIE_SALT,
        ),
        agent_transport=agent_transport,
    )

