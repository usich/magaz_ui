"""Backup Monitor — фоновая проверка наличия бэкапов 1С (Д-21).

По аналогии с Health Monitor: цикл «обход → пауза на интервал». Раз в
`backup_interval_sec` проходим по магазинам и локальным базам, где включена
проверка (`backup_check_enabled`), и опрашиваем бэкап по SMB/файловой системе.

Правила из алгоритма:

* цель — файл с датой за сегодня (или за вчера, если `backup_check_yesterday`);
* если бэкап найден — статус `ok`, в этот день больше не проверяем;
* если не найден — повторяем до `backup_max_tries` раз, между попытками не
  раньше `backup_retry_interval_sec` (по умолчанию 3 мин);
* когда попытки на день исчерпаны и бэкапа нет — статус `missing`;
* история не хранится: только текущий статус у объекта.

SMB-опрос синхронный (pysmb), поэтому выполняется в потоке (`asyncio.to_thread`),
чтобы не блокировать цикл событий.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.config import Settings
from server.core.security import SecretBox
from server.db.models import LocalBase, Store
from server.db.session import DatabaseManager
from server.services import backup_service, settings_service
from shared.timeutil import utcnow

logger = logging.getLogger(__name__)

__all__ = ["BackupMonitor", "apply_statuses", "check_missing", "should_check_now"]

#: Ключи настроек Д-21
INTERVAL_SETTING = "backup_interval_sec"
START_HOUR_SETTING = "backup_start_hour"
MAX_TRIES_SETTING = "backup_max_tries"
RETRY_INTERVAL_SETTING = "backup_retry_interval_sec"


def should_check_now(
    *,
    backup_status: str,
    backup_checked_date: str | None,
    backup_checked_at: object | None,
    backup_tries: int,
    target_date: str,
    max_tries: int,
    retry_interval_sec: int,
    now: date | None = None,
) -> tuple[bool, str]:
    """Нужно ли сейчас опрашивать объект (по правилам «раз в день + 5 попыток»).

    Возвращает `(нужно, причина)`, где причина — `ok` (уже есть), `exhausted`
    (попытки на день исчерпаны), `too_soon` (ещё не прошёл интервал повтора),
    либо пустое, когда проверять нужно.
    """
    if backup_status == "ok" and backup_checked_date == target_date:
        return False, "ok"
    if backup_checked_date != target_date:
        # Новый день: сбрасываем логику (статус остаётся, но опрашиваем заново)
        return True, ""
    if backup_tries >= max_tries:
        return False, "exhausted"
    if retry_interval_sec > 0 and backup_checked_at is not None:
        last = backup_checked_at
        import datetime as _dt

        if isinstance(last, _dt.datetime):
            delta = (_dt.datetime.now(_dt.timezone.utc) - last.astimezone(_dt.timezone.utc))
            if delta.total_seconds() < retry_interval_sec:
                return False, "too_soon"
    return True, ""

def _run_check(obj, *, secret_box: SecretBox) -> tuple[str, str | None]:
    """Проверить бэкап одного объекта (Store/LocalBase) через сервис Д-21.

    Возвращает `(status, error)`: `status` — `ok` или `missing`.
    Пароль SMB расшифровывается SecretBox-ом (§8.3), если включена авторизация.
    """
    path = obj.backup_path or ""
    try:
        password = ""
        if obj.backup_use_auth:
            password = secret_box.decrypt(obj.backup_password_enc) or ""
        result = backup_service.check_backup(
            path,
            yesterday=obj.backup_check_yesterday,
            client_machine=obj.backup_host,
            use_auth=obj.backup_use_auth,
            user=obj.backup_user,
            password=password,
        )
    except backup_service.BackupCheckError as exc:
        return "missing", str(exc)
    except Exception as exc:  # noqa: BLE001 - любой сбой опроса → «бэкапа нет»
        logger.exception("Сбой проверки бэкапа объекта id=%s", getattr(obj, "id", None))
        return "missing", str(exc)

    if result.found:
        return "ok", None
    return "missing", None


def apply_statuses(
    session: Session,
    stores: list[Store],
    local_bases: list[LocalBase],
    *,
    secret_box: SecretBox,
    max_tries: int,
    retry_interval_sec: int,
    reference: date | None = None,
) -> int:
    """Опросить объекты, где проверка включена; вернуть число опрошенных.

    Логика «раз в день + до 5 попыток с интервалом» — в `should_check_now`.
    """
    target_today = date.today() if reference is None else reference
    checked = 0

    def visit(obj, kind: str) -> None:
        nonlocal checked
        target_date = backup_service.parse_target_date(obj.backup_check_yesterday, reference=target_today)
        need, reason = should_check_now(
            backup_status=obj.backup_status or "",
            backup_checked_date=obj.backup_checked_date,
            backup_checked_at=obj.backup_checked_at,
            backup_tries=obj.backup_tries,
            target_date=target_date,
            max_tries=max_tries,
            retry_interval_sec=retry_interval_sec,
        )
        if not need:
            if reason == "exhausted" and (obj.backup_status or "") != "missing":
                obj.backup_status = "missing"
            return

        status, error = _run_check(obj, secret_box=secret_box)
        obj.backup_status = status
        obj.backup_error = error
        obj.backup_checked_at = utcnow()
        obj.backup_checked_date = target_date
        obj.backup_tries = (obj.backup_tries or 0) + 1
        checked += 1
        logger.info(
            "Бэкап %s id=%s за %s: %s%s",
            kind,
            obj.id,
            target_date,
            status,
            f" ({error})" if error else "",
        )

    for store in stores:
        if store.backup_check_enabled:
            visit(store, "магазин")
    for base in local_bases:
        if base.backup_check_enabled:
            visit(base, "база")

    return checked

def check_missing(
    session: Session,
    stores: list[Store],
    local_bases: list[LocalBase],
    *,
    secret_box: SecretBox,
    reference: date | None = None,
) -> int:
    """Ручная проверка (кнопка): опросить **только не найденные** бэкапы.

    Проверяются объекты с включённой проверкой, у которых за целевую дату ещё
    нет статуса `ok` (не найдены) — чтобы не делать лишних запросов по уже
    найденным. Интервал между попытками и час старта (10:00) здесь не учитываются:
    это явное действие администратора.
    """
    target_today = date.today() if reference is None else reference
    checked = 0

    def visit(obj, kind: str) -> None:
        nonlocal checked
        target_date = backup_service.parse_target_date(obj.backup_check_yesterday, reference=target_today)
        if obj.backup_status == "ok" and obj.backup_checked_date == target_date:
            return  # уже найден — пропускаем, лишний запрос не делаем

        status, error = _run_check(obj, secret_box=secret_box)
        obj.backup_status = status
        obj.backup_error = error
        obj.backup_checked_at = utcnow()
        obj.backup_checked_date = target_date
        obj.backup_tries = (obj.backup_tries or 0) + 1
        checked += 1
        logger.info(
            "Ручная проверка бэкапа %s id=%s за %s: %s%s",
            kind,
            obj.id,
            target_date,
            status,
            f" ({error})" if error else "",
        )

    for store in stores:
        if store.backup_check_enabled:
            visit(store, "магазин")
    for base in local_bases:
        if base.backup_check_enabled:
            visit(base, "база")

    return checked



STOP_TIMEOUT_SEC = 10


class BackupMonitor:
    """Фоновая задача опроса бэкапов (паттерн Health Monitor)."""

    def __init__(
        self,
        db: DatabaseManager,
        settings: Settings,
        *,
        secret_box: SecretBox,
        interval_override: int | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._secret_box = secret_box
        self._interval_override = interval_override
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_checked = 0

    def _setting(self, key: str) -> int:
        with self._db.session() as session:
            return settings_service.get_int(session, key)

    @property
    def interval_sec(self) -> int:
        if self._interval_override is not None:
            return max(1, int(self._interval_override))
        return max(1, self._setting(INTERVAL_SETTING))

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def poll_once(self) -> int:
        """Выполнить один обход (кнопка «Проверить сейчас», тесты)."""
        checked = await self._run_cycle()
        self.last_checked = checked
        return checked

    async def check_missing_once(self) -> int:
        """Ручная проверка (кнопка): опросить только не найденные бэкапы.

        Не зависит от часа старта и интервала между попытками — это явное действие
        администратора. Возвращает число опрошенных объектов.
        """
        with self._db.session() as session:
            stores = list(session.scalars(select(Store)).all())
            bases = list(session.scalars(select(LocalBase)).all())
            checked = check_missing(
                session,
                stores,
                bases,
                secret_box=self._secret_box,
            )
            session.commit()
        self.last_checked = checked
        return checked


    async def _run_cycle(self) -> int:
        max_tries = self._setting(MAX_TRIES_SETTING)
        retry = self._setting(RETRY_INTERVAL_SETTING)
        with self._db.session() as session:
            stores = list(session.scalars(select(Store)).all())
            bases = list(session.scalars(select(LocalBase)).all())
            checked = apply_statuses(
                session,
                stores,
                bases,
                secret_box=self._secret_box,
                max_tries=max_tries,
                retry_interval_sec=retry,
            )
            session.commit()
        return checked

    @property
    def start_hour(self) -> int:
        """Час начала запросов (по умолчанию 10:00) — раньше автоматически не опрашиваем."""
        return max(0, min(23, self._setting(START_HOUR_SETTING)))

    async def run(self) -> None:
        while not self._stop_event.is_set():
            # Д-21: запросы бэкапов начинаются с 10:00 (backup_start_hour)
            if datetime.now().hour >= self.start_hour:
                try:
                    await self.poll_once()
                except Exception:  # noqa: BLE001 - обход не падает от одной ошибки
                    logger.exception("Сбой цикла проверки бэкапов")
            else:
                logger.debug("До часа начала бэкапов (%s:00) — обход пропущен", self.start_hour)
            await self._sleep(self.interval_sec)
        logger.debug("Backup Monitor остановлен")

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=max(0.0, seconds))
        except (TimeoutError, asyncio.TimeoutError):
            pass

    def start(self) -> asyncio.Task[None]:
        if self.is_running and self._task is not None:
            return self._task
        self._stop_event.clear()
        self._task = asyncio.create_task(self.run(), name="magaz-backup-monitor")
        logger.info("Backup Monitor запущен: интервал %s с", self.interval_sec)
        return self._task

    async def stop(self) -> None:
        self._stop_event.set()
        task, self._task = self._task, None
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(task, timeout=STOP_TIMEOUT_SEC)
        except (TimeoutError, asyncio.TimeoutError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

