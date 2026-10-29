"""Engine и фабрика сессий.

SQLite требует двух вещей, без которых поведение отличается от PostgreSQL:
1. `PRAGMA foreign_keys=ON` — иначе `ON DELETE CASCADE` не работает и FK не
   проверяются (тесты на целостность §4 молча проходили бы);
2. `check_same_thread=False` — FastAPI обслуживает запросы в пуле потоков.
"""

from __future__ import annotations

from functools import lru_cache
from time import sleep
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from server.config import get_settings

#: Сколько секунд SQLite ждёт освобождения блокировки файла БД (фоновые задачи + API)
SQLITE_BUSY_TIMEOUT_SEC = 60

#: Сколько попыток записи с rollback делаем при «database is locked». WAL снимает
#: конфликты читатель/писатель, но фоновый цикл диспетчера держит write-транзакцию
#: во время HTTP-опросов, поэтому короткие коммиты API всё равно могут упереться
#: в блокировку. Пара попыток с паузой дешевле потерянной квитанции/задачи.
_SQLITE_LOCKED_RETRIES = 3
_SQLITE_LOCKED_BACKOFF_SEC = 0.4


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _record: Any) -> None:  # noqa: ANN401
    """Настроить каждое соединение SQLite для конкурентной записи.

    Важно: `PRAGMA journal_mode=WAL` здесь **намеренно не выполняется** —
    смена режима журнала требует блокировки БД и ждёт её до busy_timeout.
    На новом соединении из пула это превращалось в секундные паузы API
    («админка виснет»), потому что фоновые циклы держат write-транзакции.
    WAL выставляется один раз при старте (`ensure_sqlite_wal`).
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        # В WAL достаточно synchronous=NORMAL — меньше удержание блокировки.
        cursor.execute("PRAGMA synchronous=NORMAL")
        # Гарантированное ожидание освобождения блокировки (не полагаемся
        # только на `timeout` из connect_args).
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SEC * 1000}")
    finally:
        cursor.close()


def ensure_sqlite_wal(engine: Engine) -> None:
    """Один раз перевести файл БД в WAL — на старте, а не на каждом соединении.

    `PRAGMA journal_mode=WAL` — операция, требующая кратковременной блокировки;
    повторять её в CONNECT-хуке дорого. Режим журнала — свойство файла БД, так
    что достаточно выставить его один раз при подъёме сервера.
    """
    if not str(engine.url).startswith("sqlite"):
        return
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")


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


def commit_with_retry(session: Session) -> None:
    """Коммит сессии SQLite с повторными попытками при «database is locked».

    WAL снимает конфликт читатель/писатель, но фоновый цикл диспетчера держит
    write-транзакцию во время HTTP-опросов, поэтому короткий коммит API может
    упереться в блокировку и после busy_timeout. Вместо 500-ошибки пробуем ещё
    пару раз: после rollback прежние изменения сессии отбрасываются, поэтому
    вызывающий должен повторно собрать только финальный *лёгкий* коммит
    (flush уже выполнен в hot-точках; для тяжёлой работы флаш разбит на шаги).
    """
    _retry_locked(session, session.commit)  # type: ignore[arg-type]


def flush_with_retry(session: Session) -> None:
    """Flush сессии SQLite с повторными попытками при «database is locked».

    Применяется к коротким пачкам изменений (приём квитанций, снапшот файла),
    которые в один ход отправляются в БД: повторить их дешевле, чем отдать
    500-ошибку кассе или потерять задачу.
    """
    _retry_locked(session, session.flush)  # type: ignore[arg-type]


def _retry_locked(session: Session, operation: Any) -> None:  # noqa: ANN401
    last: OperationalError | None = None
    for attempt in range(_SQLITE_LOCKED_RETRIES):
        try:
            operation()
            return
        except OperationalError as exc:  # noqa: PERF203 - мало итераций
            message = str(exc.orig)
            if "locked" not in message.lower():
                raise
            last = exc
            session.rollback()
            if attempt + 1 < _SQLITE_LOCKED_RETRIES:
                sleep(_SQLITE_LOCKED_BACKOFF_SEC * (attempt + 1))
    # noqa: B904 - повторно поднимаем исходную SQLite-ошибку
    raise last  # type: ignore[misc]


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
