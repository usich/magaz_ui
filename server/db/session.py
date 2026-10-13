"""Engine и фабрика сессий.

SQLite требует двух вещей, без которых поведение отличается от PostgreSQL:
1. `PRAGMA foreign_keys=ON` — иначе `ON DELETE CASCADE` не работает и FK не
   проверяются (тесты на целостность §4 молча проходили бы);
2. `check_same_thread=False` — FastAPI обслуживает запросы в пуле потоков.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from server.config import get_settings

#: Сколько секунд SQLite ждёт освобождения блокировки файла БД (фоновые задачи + API)
SQLITE_BUSY_TIMEOUT_SEC = 30


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _record: Any) -> None:  # noqa: ANN401
    """Включить контроль внешних ключей для каждого соединения SQLite."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def create_db_engine(url: str, *, echo: bool = False) -> Engine:
    """Создать engine с корректными настройками для SQLite/PostgreSQL."""
    connect_args: dict[str, Any] = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        # Фон (Health Monitor, диспетчер) и запросы админа живут в одном файле
        # SQLite, поэтому ждём освобождения блокировки, а не падаем «database is
        # locked»: секунды ожидания дешевле потерянной задачи.
        connect_args["timeout"] = SQLITE_BUSY_TIMEOUT_SEC

    engine = create_engine(
        url,
        echo=echo,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


class DatabaseManager:
    """Пара «engine + sessionmaker» с единым владельцем ресурсов.

    Создаётся в `lifespan` приложения и прокидывается зависимостями (I-11),
    поэтому тесты могут поднимать несколько независимых БД в одном процессе.
    """

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        self.engine = create_db_engine(url, echo=echo)
        self.sessionmaker = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            class_=Session,
        )

    def session(self) -> Session:
        """Новая сессия (вызывающий закрывает её сам)."""
        return self.sessionmaker()

    def dispose(self) -> None:
        """Закрыть пул соединений."""
        self.engine.dispose()


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Engine по настройкам приложения (без сайд-эффектов на импорте)."""
    settings = get_settings()
    return create_db_engine(settings.database_url, echo=settings.sql_echo)


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    """Фабрика сессий по настройкам приложения."""
    engine = get_engine()
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
