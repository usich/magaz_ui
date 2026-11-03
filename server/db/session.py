"""Engine и фабрика сессий.

SQLite требует двух вещей, без которых поведение отличается от PostgreSQL:
1. `PRAGMA foreign_keys=ON` — иначе `ON DELETE CASCADE` не работает и FK не
   проверяются (тесты на целостность §4 молча проходили бы);
2. `check_same_thread=False` — FastAPI обслуживает запросы в пуле потоков.

Д-30: размер пула задаётся явно. Один сервер обслуживает сразу несколько
потребителей БД — фоновые циклы (диспетчер, мониторинг магазинов, проверка
бэкапов, обновлятор), опросы открытых вкладок SPA (`/system/info`, `/jobs`,
`/stores`, `/devices` каждые 10–15 с) и запросы агентов с касс. Умолчаний
SQLAlchemy (`pool_size=5`, `max_overflow=10`, `pool_timeout=30`) не хватало:
когда несколько запросов ждали блокировку SQLite, пул кончался, и вместо
ошибки в лог уходил `QueuePool limit of size 5 overflow 10 reached`, а вместе с
ним вставали диспетчер и админка.
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

#: Размер пула соединений (Д-30): «живых» соединений держим с запасом
DEFAULT_POOL_SIZE = 10
#: Сколько соединений разрешено взять сверх `pool_size` под пиковую нагрузку
DEFAULT_MAX_OVERFLOW = 20
#: Сколько ждать свободное соединение, прежде чем отдать `503` вместо 30-секундного
#: зависания запроса и трассировки `QueuePool limit ... reached`
DEFAULT_POOL_TIMEOUT_SEC = 15.0

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


def create_db_engine(
    url: str,
    *,
    echo: bool = False,
    pool_size: int | None = None,
    max_overflow: int | None = None,
    pool_timeout: float | None = None,
) -> Engine:
    """Создать engine с корректными настройками для SQLite/PostgreSQL.

    `pool_size`/`max_overflow`/`pool_timeout` (Д-30) передаются, только если
    заданы: у in-memory баз SQLAlchemy использует `StaticPool`/`SingletonThreadPool`,
    которым параметры очереди не подходят, а тестам они и не нужны.
    """
    connect_args: dict[str, Any] = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        # Фон (Health Monitor, диспетчер) и запросы админа живут в одном файле
        # SQLite, поэтому ждём освобождения блокировки, а не падаем «database is
        # locked»: секунды ожидания дешевле потерянной задачи.
        connect_args["timeout"] = SQLITE_BUSY_TIMEOUT_SEC

    pool_kwargs: dict[str, Any] = {}
    if not is_in_memory_sqlite(url):
        if pool_size is not None:
            pool_kwargs["pool_size"] = pool_size
        if max_overflow is not None:
            pool_kwargs["max_overflow"] = max_overflow
        if pool_timeout is not None:
            # `QueuePool.timeout` ждёт float — принимает и int, и float
            pool_kwargs["pool_timeout"] = pool_timeout

    engine = create_engine(
        url,
        echo=echo,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
        **pool_kwargs,
    )
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def is_in_memory_sqlite(url: str) -> bool:
    """БД живёт в памяти процесса (`sqlite://` или `sqlite:///:memory:`)."""
    return url.startswith("sqlite") and (":memory:" in url or url.rstrip("/") == "sqlite:")


def _read_pool_value(pool: Any, name: str, cast: Any) -> Any:  # noqa: ANN401
    """Прочитать поле пула, которое может быть и методом, и атрибутом.

    У `QueuePool` `size`/`checkedout` — методы, `timeout` — свойство, а
    `_max_overflow` — обычный атрибут; у `StaticPool`/`NullPool` части из них нет
    вовсе. Диагностика не должна падать из-за этого: не прочиталось — ключа
    просто не будет.
    """
    try:
        value = getattr(pool, name)
        if callable(value):
            value = value()
        return cast(value)
    except (AttributeError, NotImplementedError, TypeError, ValueError):
        return None


def pool_status(engine: Engine) -> dict[str, Any]:
    """Занятость пула соединений — для `/system/info` (§11.1, Д-30).

    Отдаём то, по чему видно приближение к исчерпанию: сколько соединений
    занято сейчас, сколько всего можно взять (`size + max_overflow`) и сколько
    ещё свободно. Сырое `pool.overflow()` намеренно не используется — у
    `QueuePool` оно отрицательное, пока есть свободные слоты, и в диагностике
    только путает.

    Значения читаются «мягко»: у `StaticPool`/`NullPool` нет ни размера, ни
    очереди, и диагностика не должна падать из-за этого.
    """
    pool = engine.pool
    size = _read_pool_value(pool, "size", int)
    checked_out = _read_pool_value(pool, "checkedout", int)
    max_overflow = _read_pool_value(pool, "_max_overflow", int)
    timeout = _read_pool_value(pool, "timeout", float)

    status: dict[str, Any] = {"class": type(pool).__name__}
    if size is not None:
        status["size"] = size
    if max_overflow is not None:
        status["max_overflow"] = max_overflow
    if checked_out is not None:
        status["checked_out"] = checked_out
    if size is not None and max_overflow is not None and checked_out is not None:
        status["available"] = max(0, size + max_overflow - checked_out)
    if timeout is not None:
        status["timeout_sec"] = timeout
    return status


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

    def __init__(
        self,
        url: str,
        *,
        echo: bool = False,
        pool_size: int | None = DEFAULT_POOL_SIZE,
        max_overflow: int | None = DEFAULT_MAX_OVERFLOW,
        pool_timeout: float | None = DEFAULT_POOL_TIMEOUT_SEC,
    ) -> None:
        self.url = url
        self.engine = create_db_engine(
            url,
            echo=echo,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
        )
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

    def pool_status(self) -> dict[str, Any]:
        """Занятость пула соединений (Д-30, диагностика `/system/info`)."""
        return pool_status(self.engine)

    def dispose(self) -> None:
        """Закрыть пул соединений."""
        self.engine.dispose()


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Engine по настройкам приложения (без сайд-эффектов на импорте)."""
    settings = get_settings()
    return create_db_engine(
        settings.database_url,
        echo=settings.sql_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_sec,
    )


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
