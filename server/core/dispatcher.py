"""Диспетчер операций: push, retry, очередь офлайн-магазинов (§3.2, §3.3, §11.4).

Диспетчер — вторая фоновая задача сервера (первая — Health Monitor). Он делает
то, что в v2 делал человек с `curl` (§3.3 «эталонный сценарий»), но как служба:

1. **отправка** — задачи `queued` уходят на кассу: `POST /check-files`,
   `/download-file`, `/run-merge`, `/run-update-db`, `/send-message`;
2. **опрос** — по `GET /check-merge` и `/check-update-db` (интервал
   `check_poll_interval_sec`, по умолчанию 5 с) задача переходит в
   `success`/`failed`/`timeout`, а текст лога уходит в `commands` и `job_logs`;
3. **очередь офлайн** — если магазин не отвечает, команда остаётся `queued` и
   уходит, как только касса появилась в сети (FR-16). Попытки не тратятся:
   «ждать появления в сети» — это нормальный режим, а не ошибка;
4. **retry с backoff** — для кассы, которая **в сети**, но запрос не удался
   (ошибка протокола, `5xx`): попытки считаются, пауза растёт 5 → 10 → 20 с
   (не больше 60 с), после `command_retry_max` задача закрывается `error`;
5. **push учётных данных 1С** (П-1, §8.3) — по расхождению
   `credentials_version` из `/health` сервер досылает `POST /credentials`;
6. **проверка после скачивания** (§11.3) — задача `download_file` закрывается
   успехом, когда `/check-files` реально видит скачанный файл, и заодно
   сохраняет снимок в `file_checks` (Q52) — это и есть «повторная проверка».

Урок этапа Э3 (наблюдение №7): **нельзя верить только `/check-*`** — упавший
запуск 1С не пишет лог, и `/check-merge` честно отвечает `202 in_progress`.
Поэтому при долгом `202` диспетчер дополнительно спрашивает `/health`: если
агент не занят (`busy`) и операция в `agent-state.json` не идёт, задача
закрывается `failed` с текстом причины, а не «висит» вечно.

Всё, что делает диспетчер, идемпотентно: состояние живёт в БД (`commands`),
поэтому перезапуск сервера не теряет задачи (FR-24) — незавершённые команды
подхватываются заново.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select

from server.config import Settings
from server.core.client_http import (
    AgentAuthError,
    AgentClient,
    AgentError,
    AgentOfflineError,
    AgentProtocolError,
    AgentTarget,
    build_agent_target,
)
from server.core.security import SecretBox
from server.db.models import Command, FileCheck, Store
from server.db.session import DatabaseManager, commit_with_retry, flush_with_retry
from server.services import (
    command_service,
    device_service,
    notification_service,
    settings_service,
)
from shared.enums import (
    CommandKind,
    CommandStatus,
    JobLogLevel,
    NotificationSeverity,
    TargetType,
)
from shared.paths import is_extension_operation
from shared.schemas import CheckFilesResponse, CredentialsPush, NotifyCancelRequest
from shared.timeutil import utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "BACKOFF_BASE_SEC",
    "BACKOFF_MAX_SEC",
    "BUSY_GRACE_SEC",
    "CommandDispatcher",
    "DispatchReport",
    "STOP_TIMEOUT_SEC",
    "expected_file_kind",
    "extract_file_name",
]

#: Ключи настроек §4.9, от которых зависит диспетчер
POLL_INTERVAL_SETTING = "check_poll_interval_sec"
RETRY_MAX_SETTING = "command_retry_max"
POLL_TIMEOUT_SETTING = "command_poll_timeout_sec"
ONLINE_WINDOW_SETTING = "online_window_sec"
CREDENTIALS_INTERVAL_SETTING = "credentials_push_interval_sec"
DEFAULT_FILE_NAME_SETTING = "default_file_name"

#: Backoff повторной отправки: 5 → 10 → 20 … но не больше минуты
BACKOFF_BASE_SEC = 5.0
BACKOFF_MAX_SEC = 60.0
#: Сколько ждём «запустилась ли 1С», прежде чем считать задачу упавшей (наблюдение №7)
BUSY_GRACE_SEC = 30.0
#: Ограничение ожидания при остановке сервера
STOP_TIMEOUT_SEC = 5.0

#: Расширение файла → колонка снимка `file_checks` (§4.5, Q52)
FILE_KIND_BY_SUFFIX: dict[str, str] = {".cf": "cf", ".cfe": "cfe", ".xml": "xml"}

#: Задачи, у которых состояние читается через `/check-*` (Приложение B)
POLLABLE_KINDS: frozenset[str] = frozenset(
    {str(CommandKind.RUN_MERGE), str(CommandKind.RUN_UPDATE_DB)}
)

def extract_file_name(url_file: str) -> str:
    """Имя файла без расширения из ссылки — как его понимает `/check-files` (FR-2).

    `basename` берётся из URL, а не из строки целиком: ссылки Яндекс.Диска
    содержат параметры, а имя файла должно остаться «голым» (I-2).
    """
    path = urlparse(url_file or "").path or (url_file or "")
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        return name
    return stem if f".{suffix.casefold()}" in FILE_KIND_BY_SUFFIX else name


def expected_file_kind(url_file: str) -> str | None:
    """Какая колонка `file_checks` подтвердит скачивание (`cf`/`cfe`/`xml`)."""
    path = urlparse(url_file or "").path or (url_file or "")
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    _, _, suffix = name.rpartition(".")
    return FILE_KIND_BY_SUFFIX.get(f".{suffix.casefold()}")


def _stem_of_downloaded(name: str) -> str | None:
    """Имя без расширения для реально скачанного файла (например `update.cf` → `update`).

    Возвращает `None`, если файл не является пакетом обновления (`.cf`/`.cfe`/
    `.xml`) — такой файл `/check-files` увидеть не может, ждать его бессмысленно.
    """
    _, dot, suffix = name.rpartition(".")
    if not dot or f".{suffix.casefold()}" not in FILE_KIND_BY_SUFFIX:
        return None
    return name[: len(name) - len(suffix) - 1]


def _downloaded(response: CheckFilesResponse, kind: str | None) -> bool:
    """Появился ли скачанный файл в рабочей папке агента (§11.3)."""
    if kind == "cf":
        return bool(response.cf_exists)
    if kind == "cfe":
        return bool(response.cfe_exists)
    if kind == "xml":
        return bool(response.xml_exists)
    return bool(response.cf_exists or response.cfe_exists or response.xml_exists)


def _parse_mtime(value: str | None) -> datetime | None:
    """`*_mtime` из ответа агента (UTC с `Z`) — в `datetime` для БД (§6)."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def apply_file_check(
    session: Any,  # noqa: ANN401 - Session
    store: Store,
    filename: str,
    response: CheckFilesResponse,
    *,
    checked_at: datetime | None = None,
) -> FileCheck:
    """Сохранить снимок проверки файлов (§4.5, Q52).

    В UI показывается **последний** снимок на магазин, поэтому «история» копится
    строкой на проверку, а выборку делает API (`/files/matrix`).
    """
    snapshot = FileCheck(
        store_id=store.id,
        filename=filename,
        cf_exists=bool(response.cf_exists),
        cfe_exists=bool(response.cfe_exists),
        xml_exists=bool(response.xml_exists),
        cf_size=response.cf_size,
        cfe_size=response.cfe_size,
        xml_size=response.xml_size,
        cf_mtime=_parse_mtime(response.cf_mtime),
        cfe_mtime=_parse_mtime(response.cfe_mtime),
        xml_mtime=_parse_mtime(response.xml_mtime),
        checked_at=checked_at or utcnow(),
    )
    session.add(snapshot)
    # Снимок пишется из фонового цикла параллельно с Health Monitor и API:
    # при блокировке файла БД повторяем запись, а не роняем задачу.
    flush_with_retry(session)
    return snapshot


@dataclass(slots=True)
class DispatchReport:
    """Итог одного цикла диспетчера (диагностика и тесты)."""

    started_at: datetime
    finished_at: datetime | None = None
    sent: int = 0
    polled: int = 0
    succeeded: int = 0
    failed: int = 0
    retried: int = 0
    waiting: int = 0
    credentials_pushed: int = 0
    devices_pushed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def touched(self) -> int:
        """Сколько задач цикл реально тронул (отправка + опрос)."""
        return self.sent + self.polled



class CommandDispatcher:
    """Фоновая задача «отправить → опросить → закрыть» для всех задач (§3.2)."""

    def __init__(
        self,
        db: DatabaseManager,
        settings: Settings,
        *,
        transport: Any = None,  # noqa: ANN401 - httpx.AsyncBaseTransport | None
        secret_box: SecretBox | None = None,
        interval_override_sec: float | None = None,
        startup_delay_sec: float | None = None,
        backoff_base_sec: float | None = None,
        busy_grace_sec: float | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._transport = transport
        self._secret_box = secret_box
        self._interval_override = interval_override_sec
        self._startup_delay = (
            settings.dispatcher_startup_delay_sec
            if startup_delay_sec is None
            else startup_delay_sec
        )
        self._backoff_base = BACKOFF_BASE_SEC if backoff_base_sec is None else backoff_base_sec
        self._busy_grace = BUSY_GRACE_SEC if busy_grace_sec is None else busy_grace_sec
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        #: command_id → время, раньше которого повторять отправку не нужно
        self._backoff: dict[int, datetime] = {}
        #: command_id → последнее сообщение опроса (чтобы не дублировать журнал)
        self._progress: dict[int, str] = {}
        #: store_id → когда последний раз сверяли версии на кассе (П-1, Д-1)
        self._credentials_checked: dict[int, datetime] = {}
        #: command_id задач, переведённых в `SENT` «аварийно» (Вариант B): `/run-*`
        #: мог дойти до агента, но ответ не дошёл (разрыв). Их опрашиваем, а не
        #: переотправляем; если опрос покажет, что агент не знает об операции —
        #: вернём в `queued` для повторной отправки (§13.2, FR-9).
        self._provisional_sent: set[int] = set()
        self.last_report: DispatchReport | None = None

    # --- Настройки из БД ----------------------------------------------------

    def _setting(self, session: Any, key: str) -> int:  # noqa: ANN401 - Session
        """Целочисленная настройка §4.9."""
        return settings_service.get_int(session, key)

    @property
    def interval_sec(self) -> float:
        """Интервал цикла: `check_poll_interval_sec` (§4.9, по умолчанию 5 с)."""
        if self._interval_override is not None:
            return max(0.05, float(self._interval_override))
        with self._db.session() as session:
            return max(1.0, float(self._setting(session, POLL_INTERVAL_SETTING)))

    @property
    def is_running(self) -> bool:
        """Задача запущена и ещё не завершилась."""
        return self._task is not None and not self._task.done()

    # --- Жизненный цикл -----------------------------------------------------

    async def run(self) -> None:
        """Цикл «обработать задачи → пауза» до сигнала остановки."""
        if self._startup_delay > 0:
            await self._sleep(self._startup_delay)

        while not self._stop_event.is_set():
            try:
                await self.process_once()
            except Exception:  # noqa: BLE001 - диспетчер не падает от одной ошибки
                logger.exception("Сбой цикла диспетчера")
            await self._sleep(self.interval_sec)

        logger.debug("Диспетчер остановлен")

    async def _sleep(self, seconds: float) -> None:
        """Пауза, прерываемая остановкой сервера."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=max(0.0, seconds))
        except (TimeoutError, asyncio.TimeoutError):
            pass

    def start(self) -> asyncio.Task[None]:
        """Запустить фоновую задачу (идемпотентно)."""
        if self.is_running and self._task is not None:
            return self._task
        self._stop_event.clear()
        self._task = asyncio.create_task(self.run(), name="magaz-command-dispatcher")
        logger.info("Диспетчер запущен: интервал опроса %s с", self.interval_sec)
        return self._task

    async def stop(self) -> None:
        """Остановить задачу и дождаться её завершения."""
        self._stop_event.set()
        task, self._task = self._task, None
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(task, timeout=STOP_TIMEOUT_SEC)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("Диспетчер не завершился за %s с — отменяем задачу", STOP_TIMEOUT_SEC)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # --- Один цикл ----------------------------------------------------------

    async def process_once(self) -> DispatchReport:
        """Обработать незавершённые задачи: отправить `queued`, опросить остальные."""
        report = DispatchReport(started_at=utcnow())
        session = self._db.session()
        try:
            pending = command_service.list_pending(session)
            seen: set[int] = set()
            for command in pending:
                seen.add(command.id)
                store = command.store
                if store is None or not store.is_active:
                    command_service.add_log(
                        session,
                        command,
                        "Магазин отключён — задача отменена",
                        level=JobLogLevel.WARNING,
                    )
                    command_service.set_status(session, command, CommandStatus.CANCELED)
                    report.failed += 1
                    continue
                if self._in_backoff(command.id):
                    report.waiting += 1
                    continue
                if self._expired(session, command):
                    self._finish(
                        session,
                        command,
                        CommandStatus.TIMEOUT,
                        message=f"Истёк предел ожидания операции ({command.kind})",
                        report=report,
                    )
                    continue
                command_id = command.id
                try:
                    if str(command.status) == str(CommandStatus.QUEUED):
                        await self._send(session, store, command, report)
                    else:
                        await self._poll(session, store, command, report)
                except AgentError as exc:
                    self._handle_error(session, store, command, exc, report)
                except Exception:  # noqa: BLE001 - одна задача не роняет цикл
                    # Ошибка БД (например, «database is locked») оставляет сессию
                    # в PendingRollbackError: без rollback любое обращение к её
                    # объектам (и финальный commit) падало вторично.
                    session.rollback()
                    logger.exception("Неожиданный сбой обработки задачи id=%s", command_id)
                    report.errors.append(f"Задача {command_id}: внутренняя ошибка")
            self._prune(seen)
            await self._sync_pushes(session, report)
            commit_with_retry(session)
        finally:
            session.close()

        report.finished_at = utcnow()
        self.last_report = report
        return report

    # --- Backoff и признаки -------------------------------------------------

    def _in_backoff(self, command_id: int) -> bool:
        """Пора ли повторять задачу (retry с backoff, §13.2)."""
        until = self._backoff.get(command_id)
        return until is not None and until > utcnow()

    def _schedule_backoff(self, command_id: int, attempts: int) -> None:
        """Отложить повтор: 5 → 10 → 20 с, но не больше минуты."""
        delay = min(self._backoff_base * (2 ** max(0, attempts - 1)), BACKOFF_MAX_SEC)
        self._backoff[command_id] = utcnow() + timedelta(seconds=delay)

    def _prune(self, seen: set[int]) -> None:
        """Забыть счётчики по задачам, которых больше нет в очереди (без утечек)."""
        for command_id in list(self._backoff):
            if command_id not in seen:
                del self._backoff[command_id]
        for command_id in list(self._progress):
            if command_id not in seen:
                del self._progress[command_id]
        self._provisional_sent &= seen

    def _expired(self, session: Any, command: Command) -> bool:  # noqa: ANN401 - Session
        """Не «висит» ли задача дольше `command_poll_timeout_sec` (§11.4).

        Ожидание офлайн-кассы таймаутом не считается: `queued` — это очередь
        (FR-16), а не зависшая операция.
        """
        if str(command.status) == str(CommandStatus.QUEUED):
            return False
        limit = self._setting(session, POLL_TIMEOUT_SETTING)
        # Сравнение нестрогое: лимит `0` означает «не ждать вовсе», а часы Windows
        # имеют разрешение ~15 мс — при строгом `>` задача, отправленная и проверенная
        # в одном тике, оставалась «running» (и тест таймаута был недетерминированным)
        return command_service.status_age_sec(command) >= float(limit)

    def _store_online(self, session: Any, store: Store) -> bool:  # noqa: ANN401 - Session
        """В сети ли магазин по окну `online_window_sec` (§3.3)."""
        window = self._setting(session, ONLINE_WINDOW_SETTING)
        return store.is_online(utcnow(), window)

    def _client(self, store: Store) -> AgentClient:
        """Клиент агента с таймаутом сервера и подменённым транспортом в тестах."""
        target: AgentTarget = build_agent_target(store, self._settings)
        return AgentClient(
            target,
            timeout=float(self._settings.agent_request_timeout_sec),
            transport=self._transport,
        )

    # --- Отправка (`queued` → касса) ----------------------------------------

    async def _send(
        self,
        session: Any,  # noqa: ANN401 - Session
        store: Store,
        command: Command,
        report: DispatchReport,
    ) -> None:
        """Отправить задачу на кассу своим путём для каждого типа (§5.1.1)."""
        kind = str(command.kind)
        async with self._client(store) as client:
            if kind == str(CommandKind.CHECK_FILES):
                await self._send_check_files(session, store, command, client, report)
            elif kind == str(CommandKind.DOWNLOAD_FILE):
                await self._send_download(session, command, client, report)
            elif kind in (str(CommandKind.RUN_MERGE), str(CommandKind.RUN_UPDATE_DB)):
                await self._send_1c(session, store, command, client, report)
            elif kind == str(CommandKind.SEND_MESSAGE):
                await self._send_message(session, command, client, report)
            elif kind == str(CommandKind.DELETE_FILE):
                await self._send_delete_files(session, store, command, client, report)
            elif kind == str(CommandKind.NOTIFY):
                await self._send_notify(session, store, command, client, report)
            elif kind == str(CommandKind.NOTIFY_CANCEL):
                await self._send_notify_cancel(session, store, command, client, report)
            else:
                self._finish(
                    session,
                    command,
                    CommandStatus.ERROR,
                    message=f"Тип задачи {kind!r} на этом этапе не выполняется",
                    report=report,
                )

    def _register_send(self, session: Any, command: Command, report: DispatchReport) -> None:  # noqa: ANN401
        """Отметить факт отправки: `sent` + попытка (§4.3 `attempts`)."""
        command_service.set_status(session, command, CommandStatus.SENT)
        command_service.register_attempt(session, command)
        report.sent += 1

    def _default_file_name(self, session: Any) -> str:  # noqa: ANN401 - Session
        """Имя файла по умолчанию из настроек §4.9 (`update`)."""
        try:
            return settings_service.get_str(session, DEFAULT_FILE_NAME_SETTING)
        except Exception:  # noqa: BLE001 - настройка не критична
            return "update"

    async def _send_check_files(  # noqa: PLR0913 - параметры цикла
        self,
        session: Any,  # noqa: ANN401 - Session
        store: Store,
        command: Command,
        client: AgentClient,
        report: DispatchReport,
    ) -> None:
        """`POST /check-files`: снимок файлов сразу попадает в `file_checks` (§4.5)."""
        filename = str((command.params or {}).get("filename") or self._default_file_name(session))
        response = await client.check_files(filename)
        self._register_send(session, command, report)

        apply_file_check(session, store, filename, response)
        present = [
            name
            for name, exists in (
                ("cf", response.cf_exists),
                ("cfe", response.cfe_exists),
                ("xml", response.xml_exists),
            )
            if exists
        ]
        found = ", ".join(present) if present else "ничего не найдено"
        self._finish(
            session,
            command,
            CommandStatus.SUCCESS,
            message=f"Файлы «{filename}»: {found}",
            report=report,
            level=JobLogLevel.INFO if present else JobLogLevel.WARNING,
        )

    async def _send_delete_files(  # noqa: PLR0913 - параметры цикла
        self,
        session: Any,  # noqa: ANN401 - Session
        store: Store,
        command: Command,
        client: AgentClient,
        report: DispatchReport,
    ) -> None:
        """`POST /files/delete`: удалить файлы обновления на кассе (Э10, §11.3).

        Выполняется прямо в HTTP-запросе, как `send_message`: как только касса
        ответила — задача закрывается результатом. Затем диспетчер перечитывает
        снимок (`/check-files`), поэтому матрица `/files` показывает факт сразу,
        без ручного «Проверить» (Q52).
        """
        params = command.params or {}
        file_name = str(params.get("file_name") or self._default_file_name(session))
        suffix = str(params.get("suffix") or "").strip() or None
        response = await client.delete_files(file_name, suffix=suffix)
        self._register_send(session, command, report)

        done = ", ".join(response.removed) if response.removed else "ничего не удалено"
        missing = f"; не было: {', '.join(response.missing)}" if response.missing else ""
        self._finish(
            session,
            command,
            CommandStatus.SUCCESS,
            message=f"Файлы «{file_name}»: удалено {done}{missing}",
            report=report,
            level=JobLogLevel.INFO if response.removed else JobLogLevel.WARNING,
        )
        try:
            snapshot = await client.check_files(file_name)
        except AgentError as exc:
            logger.info("Снимок после удаления «%s» не получен: %s", file_name, exc)
            return
        apply_file_check(session, store, file_name, snapshot)

    async def _send_download(
        self,
        session: Any,  # noqa: ANN401 - Session
        command: Command,
        client: AgentClient,
        report: DispatchReport,
    ) -> None:
        """`POST /download-file` (ФН2): дальше ждём файл в рабочей папке (§11.3)."""
        url = str((command.params or {}).get("url-file") or "").strip()
        body = await client.download_file(url)
        self._register_send(session, command, report)

        expected = extract_file_name(url)
        command.log_files = [expected]
        command_service.set_status(session, command, CommandStatus.RUNNING)
        command_service.add_log(
            session,
            command,
            f"Скачивание запущено: {expected} ({body.get('message', 'принято агентом')})",
        )

    async def _send_1c(
        self,
        session: Any,  # noqa: ANN401 - Session
        store: Store,
        command: Command,
        client: AgentClient,
        report: DispatchReport,
    ) -> None:
        """`POST /run-merge` и `POST /run-update-db` — постановка операции (FR-9)."""
        payload = build_1c_payload(store, command)
        if str(command.kind) == str(CommandKind.RUN_MERGE):
            body = await client.run_merge(payload)
        else:
            body = await client.run_update_db(payload)
        self._register_send(session, command, report)

        log_path = str(body.get("log_path") or "")
        if log_path:
            command.log_files = [log_path.replace("\\", "/").rsplit("/", 1)[-1]]
        message = body.get("message") or "Операция принята агентом"
        command_service.add_log(
            session, command, f"{describe_kind(str(command.kind))}: {message}"
        )

    async def _send_message(
        self,
        session: Any,  # noqa: ANN401 - Session
        command: Command,
        client: AgentClient,
        report: DispatchReport,
    ) -> None:
        """`POST /send-message`: окно `msg` + завершение `1cv8.exe` (FR-8)."""
        text = str((command.params or {}).get("text_message") or "")
        body = await client.send_message(text)
        self._register_send(session, command, report)
        killed = "да" if body.get("killed_1cv8") else "нет"
        self._finish(
            session,
            command,
            CommandStatus.SUCCESS,
            message=f"Сообщение отправлено пользователям (1cv8.exe завершён: {killed})",
            report=report,
        )

    async def _send_notify(
        self,
        session: Any,  # noqa: ANN401 - Session
        store: Store,
        command: Command,
        client: AgentClient,
        report: DispatchReport,
    ) -> None:
        """`POST /notify`: уведомление уходит в файловую очередь кассы (ФН3, §7.4).

        Это не операция 1С: агент отвечает сразу, а показ в сессиях делает
        TrayAgent. Отменённую рассылку не отправляем вовсе — задача закрывается
        `canceled` без попыток (§11.5). Ответ агента несёт число видимых сессий:
        оно попадает в `notification_targets` (§4.7).
        """
        try:
            notification_id = int((command.params or {}).get("notification_id") or 0)
        except (TypeError, ValueError):
            notification_id = 0

        if notification_id <= 0:
            self._finish(
                session,
                command,
                CommandStatus.ERROR,
                message="В задаче нет id уведомления — отправлять нечего",
                report=report,
                level=JobLogLevel.ERROR,
            )
            return

        try:
            notification = notification_service.get_notification(session, notification_id)
        except notification_service.NotificationNotFoundError as exc:
            self._finish(
                session,
                command,
                CommandStatus.ERROR,
                message=str(exc),
                report=report,
                level=JobLogLevel.ERROR,
            )
            return

        if not notification.is_active:
            self._finish(
                session,
                command,
                CommandStatus.CANCELED,
                message="Уведомление отменено до отправки на кассу",
                report=report,
            )
            return

        payload = notification_service.push_payload(session, store, notification)
        body = await client.notify(payload)
        self._register_send(session, command, report)

        sessions = 0
        try:
            sessions = int(body.get("sessions") or 0)
        except (TypeError, ValueError):
            sessions = 0
        notification_service.mark_delivered(session, store, notification, sessions=sessions)
        self._finish(
            session,
            command,
            CommandStatus.SUCCESS,
            message=f"Уведомление доставлено на кассу (сессий на кассе: {sessions})",
            report=report,
        )

    async def _send_notify_cancel(
        self,
        session: Any,  # noqa: ANN401 - Session
        store: Store,
        command: Command,
        client: AgentClient,
        report: DispatchReport,
    ) -> None:
        """`POST /notify/cancel`: убрать отменённое уведомление из очереди кассы (1.23, §7.4).

        Админ отменил/удалил уведомление: касса должна перестать его показывать.
        Задача ставится только магазинам, куда уведомление уже доставлено, поэтому
        `notification_id` здесь обязателен; недоступная касса оставляет задачу в
        очереди и получает отзыв, когда вернётся в сеть (FR-16).
        """
        try:
            notification_id = int((command.params or {}).get("notification_id") or 0)
        except (TypeError, ValueError):
            notification_id = 0

        if notification_id <= 0:
            self._finish(
                session,
                command,
                CommandStatus.ERROR,
                message="В задаче отзыва нет id уведомления — отправлять нечего",
                report=report,
                level=JobLogLevel.ERROR,
            )
            return

        payload = NotifyCancelRequest(notification_id=notification_id)
        await client.notify_cancel(payload)
        self._register_send(session, command, report)
        self._finish(
            session,
            command,
            CommandStatus.SUCCESS,
            message=f"Уведомление id={notification_id} отозвано с кассы",
            report=report,
        )

    # --- Опрос отправленных задач -------------------------------------------

    async def _poll(
        self,
        session: Any,  # noqa: ANN401 - Session
        store: Store,
        command: Command,
        report: DispatchReport,
    ) -> None:
        """Опрос состояния уже отправленной задачи."""
        kind = str(command.kind)
        if kind in POLLABLE_KINDS:
            await self._poll_1c(session, store, command, report)
        elif kind == str(CommandKind.DOWNLOAD_FILE):
            await self._poll_download(session, store, command, report)
        else:
            # `check_files` / `send_message` завершаются сразу при отправке:
            # если такая задача осталась в `sent` (сервер перезапустился в этот
            # момент), её нужно просто отправить заново (FR-24).
            command_service.set_status(session, command, CommandStatus.QUEUED)
            report.waiting += 1

    async def _poll_1c(  # noqa: PLR0913 - параметры цикла
        self,
        session: Any,  # noqa: ANN401 - Session
        store: Store,
        command: Command,
        report: DispatchReport,
    ) -> None:
        """`GET /check-merge` / `GET /check-update-db`: `202 → 200/400` (§3.3).

        Плюс защита от «залипшего `202`»: если касса не занята и в состоянии
        операции пусто, значит запуск не состоялся — задача закрывается
        `failed` с причиной (наблюдение №7 этапа Э3).
        """
        kind = str(command.kind)
        # FR-4/Д-16/Д-17: имя расширения может жить в карточке магазина, а в `params`
        # операции его нет. Флаг `extension` для `/check-*` обязан отражать
        # фактическую операцию (как в `build_1c_payload`), иначе агент читал бы
        # основной лог, а команда пишет в `<...>-extension.txt` — и сервер не
        # находит результат. Тип решает `is_extension_operation`: явный флаг из
        # params → файл (`.cf`/`.cfe`) → `extension_name` (Д-16, Д-17).
        params = command.params or {}
        extension = is_extension_operation(
            params.get("is_extension"),
            str(params.get("file_name") or ""),
            params.get("extension_name") or store.extension_name,
        )
        async with self._client(store) as client:
            status_code, body = await client.check_job_state(kind, extension=extension)
            report.polled += 1
            message = str(body.get("message") or "")
            raw_logs = body.get("logs")
            logs_text: str | None = raw_logs if isinstance(raw_logs, str) else None

            if status_code == 200:
                self._finish(
                    session,
                    command,
                    CommandStatus.SUCCESS,
                    message=message or "Операция завершена успешно",
                    log_text=logs_text,
                    report=report,
                )
                self._maybe_notify_can_login(session, store, command)
                return
            if status_code == 400:
                self._finish(
                    session,
                    command,
                    CommandStatus.FAILED,
                    message=message or "Операция завершилась ошибкой (см. лог)",
                    log_text=logs_text,
                    report=report,
                    level=JobLogLevel.ERROR,
                )
                return

            command_service.set_status(session, command, CommandStatus.RUNNING)
            self._log_progress(session, command, message)
            stuck = await self._stuck_reason(client, command)
            if stuck:
                if (
                    command.id in self._provisional_sent
                    and "запуск не состоялся" in stuck
                ):
                    # Вариант B (Д-15): задачу перевели в `SENT` «аварийно» (разрыв
                    # на отправке), а опрос показал, что агент не знает об операции
                    # (не занят, лог пуст) — команда на самом деле не дошла. Возвращаем
                    # в очередь для честной повторной отправки, а не закрываем failed.
                    self._provisional_sent.discard(command.id)
                    command_service.set_status(session, command, CommandStatus.QUEUED)
                    command_service.add_log(
                        session,
                        command,
                        "Агент не подтвердил запуск операции — задача возвращена "
                        "в очередь для повторной отправки",
                        level=JobLogLevel.WARNING,
                    )
                    report.waiting += 1
                else:
                    self._finish(
                        session,
                        command,
                        CommandStatus.FAILED,
                        message=stuck,
                        log_text=logs_text,
                        report=report,
                        level=JobLogLevel.ERROR,
                    )
            else:
                report.waiting += 1

    def _maybe_notify_can_login(
        self, session: Any, store: Store, command: Command  # noqa: ANN401 - Session
    ) -> None:
        """Д-18: после успешного **полного** обновления БД сообщить «Можно входить».

        Срабатывает, когда `run_update_db` с `is_fast_update=false` (полный цикл:
        завершение сеансов → `/UpdateDBCfg` → разрешение входа) завершился успехом.
        Создаём уведомление уровня «важно» (без подтверждения, удаляется после
        прочтения, без категории) и ставим `notify`-задачу на этот магазин — касса
        покажет его кассирам. Один раз: задача уже закрыта и больше не опрашивается.
        """
        if str(command.kind) != str(CommandKind.RUN_UPDATE_DB):
            return
        params = command.params or {}
        if bool(params.get("is_fast_update")):
            return
        try:
            notification = notification_service.create_notification(
                session,
                title="Можно входить в программу 1С",
                body="Можно входить в программу 1С",
                severity=str(NotificationSeverity.IMPORTANT),
                requires_ack=False,
                delete_after_read=True,
                target_type=str(TargetType.STORES),
                stores=[store],
                created_by="dispatcher",
            )
            command_service.create_command(
                session,
                store_id=store.id,
                kind=CommandKind.NOTIFY,
                params={"notification_id": notification.id},
                created_by="dispatcher",
            )
            command_service.add_log(
                session,
                command,
                f"Уведомление «Можно входить в программу 1С» (id={notification.id}) "
                "поставлено на кассу после обновления",
            )
        except Exception:  # noqa: BLE001 - уведомление не роняет цикл диспетчера
            logger.exception(
                "Не удалось создать уведомление «Можно входить» для магазина id=%s", store.id
            )

    def _log_progress(self, session: Any, command: Command, message: str) -> None:  # noqa: ANN401
        """Записать в журнал задачи только **новое** сообщение опроса (§4.4)."""
        if not message or self._progress.get(command.id) == message:
            return
        self._progress[command.id] = message
        command_service.add_log(session, command, f"Операция идёт: {message}")

    async def _stuck_reason(self, client: AgentClient, command: Command) -> str | None:
        """Почему `202` не меняется: касса не занята → запуск не состоялся (§7.3)."""
        if command_service.status_age_sec(command) < self._busy_grace:
            return None
        try:
            health = await client.get_health()
        except AgentError as exc:
            logger.debug(
                "Не удалось уточнить состояние задачи id=%s: %s", command.id, exc.message
            )
            return None
        if health.interrupted_operation:
            return (
                "Операция на кассе прервана перезагрузкой/остановкой службы: "
                f"{health.interrupted_operation}"
            )
        if not (health.busy.merge or health.busy.update):
            return (
                "1С-операция на кассе не выполняется, а лог операции пуст — "
                "запуск не состоялся (подробности в agent-state.json кассы)"
            )
        return None

    async def _poll_download(
        self,
        session: Any,  # noqa: ANN401 - Session
        store: Store,
        command: Command,
        report: DispatchReport,
    ) -> None:
        """Дождаться скачанного файла и заказать повторную проверку (§11.3)."""
        url = str((command.params or {}).get("url-file") or "")
        filename = extract_file_name(url) or self._default_file_name(session)
        file_kind = expected_file_kind(url)

        async with self._client(store) as client:
            response = await client.check_files(filename)
            report.polled += 1

        # Имя из ссылки может не совпадать с реально скачанным файлом: для
        # ссылок Яндекс.Диска basename — это id ресурса (`.../d/ukPxhdZu6Ax4WQ`),
        # а не имя пакета. Агент сам знает имя из `last-download.json` и сообщает
        # его в `downloaded` — переспросим по нему (§11.3, FR-1).
        if not _downloaded(response, file_kind) and response.downloaded:
            real_stem = _stem_of_downloaded(response.downloaded)
            if real_stem and real_stem != filename:
                filename = real_stem
                file_kind = expected_file_kind(response.downloaded)
                async with self._client(store) as client:
                    response = await client.check_files(filename)
                    report.polled += 1

        # Урок Э3 (наблюдение №7) — «нельзя верить только `/check-*`», но и
        # ждать файл бесконечно нельзя: скачивание на кассе могло провалиться
        # (сетевой сбой, Диск не отдал ссылку). Агент пишет `status: failed` в
        # `last-download.json` и сообщает его в `/check-files`. Закрываем задачу
        # как `failed` с текстом ошибки, а не «висим» до таймаута (§11.4).
        if str(response.download_status or "") == "failed":
            reason = response.download_error or "неизвестная ошибка при скачивании"
            self._finish(
                session,
                command,
                CommandStatus.FAILED,
                message=f"Скачивание файла с Яндекс.Диска не удалось: {reason}",
                report=report,
            )
            return

        if not _downloaded(response, file_kind):
            report.waiting += 1
            self._log_progress(session, command, "файл ещё не появился в рабочей папке кассы")
            return

        apply_file_check(session, store, filename, response)
        self._finish(
            session,
            command,
            CommandStatus.SUCCESS,
            message=f"Файл «{filename}» скачан и виден в рабочей папке кассы",
            report=report,
        )
        # ФН2 + §11.3: «→ затем повторная проверка». Отдельной задачей, чтобы
        # повторная проверка была видна в истории, а не «внутри» скачивания.
        follow_up = command_service.create_command(
            session,
            store_id=store.id,
            kind=CommandKind.CHECK_FILES,
            params={"filename": filename},
            created_by="dispatcher",
        )
        command_service.add_log(
            session,
            follow_up,
            f"Повторная проверка после скачивания (задача {command.id})",
        )

    # --- Ошибки отправки и опроса -------------------------------------------

    def _handle_error(  # noqa: PLR0913 - параметры цикла
        self,
        session: Any,  # noqa: ANN401 - Session
        store: Store,
        command: Command,
        exc: AgentError,
        report: DispatchReport,
    ) -> None:
        """Решить судьбу задачи после ошибки канала (§13.2).

        Четыре разных случая, и путать их нельзя:

        * **касса офлайн** — команда остаётся `queued` и ждёт появления в сети
          (FR-16): попытки не тратятся, иначе очередь «сгорала» бы за 15 секунд;
        * **`409` от агента** — на кассе уже идёт другая 1С-операция (I-13):
          ждём с максимальной паузой, команда не теряется и попыток не тратит;
        * **разрыв/таймаут на «в сеть» кассе для `/run-*`** (Вариант B, Д-15):
          неопределённость — агент мог принять и выполнить, но ответ не дошёл.
          Допрашиваем `/check-*` (переведя задачу в `SENT` при неудачной отправке),
          а не переотправляем, иначе на свободной кассе операция выполнится дважды;
        * **касса в сети, но запрос не удался** — считаем попытку (§4.3
          `attempts`), повторяем с backoff, после `command_retry_max` — `error`.
        """
        command.last_error = exc.message
        online = self._store_online(session, store)

        if isinstance(exc, AgentAuthError):
            # Токен/IP отвергнуты: повторять бессмысленно, нужен админ (§8.1)
            self._finish(
                session,
                command,
                CommandStatus.ERROR,
                message=f"Агент отклонил запрос ({exc.status_code}): {exc.message}",
                report=report,
                level=JobLogLevel.ERROR,
            )
            return

        if isinstance(exc, AgentOfflineError) and not online:
            command_service.set_status(session, command, CommandStatus.QUEUED)
            session.flush()
            report.waiting += 1
            logger.debug(
                "Задача id=%s ждёт появления магазина id=%s в сети", command.id, store.id
            )
            return

        if isinstance(exc, AgentProtocolError) and exc.status_code == 409:
            command_service.set_status(session, command, CommandStatus.QUEUED)
            command_service.add_log(
                session,
                command,
                "На кассе уже выполняется 1С-операция (409) — ждём освобождения",
                level=JobLogLevel.WARNING,
            )
            self._schedule_backoff(command.id, attempts=99)  # максимальная пауза
            report.waiting += 1
            return

        # Вариант B (Д-15, §13.2): для `/run-merge`/`/run-update-db` разрыв или
        # таймаут на «в сеть» кассе — это неопределённость, а не гарантированный
        # провал доставки: агент мог принять команду и выполнить, но ответ не
        # дошёл. Вместо повторной отправки (которая дала бы дубль операции на
        # свободной кассе) — допрашиваем `/check-*`. Если отправка не удалась
        # (`queued`), переводим задачу в `SENT`; при обрыве опроса оставляем как
        # есть. Возврат в `queued` произойдёт только если опрос покажет, что
        # агент не знает об операции.
        is_1c_kind = str(command.kind) in POLLABLE_KINDS
        if is_1c_kind and isinstance(exc, AgentOfflineError) and online:
            sending = str(command.status) == str(CommandStatus.QUEUED)
            if sending:
                self._provisional_sent.add(command.id)
                command_service.set_status(session, command, CommandStatus.SENT)
                command_service.add_log(
                    session,
                    command,
                    f"Ответ агента на отправку не получен ({exc.message}) — "
                    "опрос, а не повторная отправка (защита от дубля)",
                    level=JobLogLevel.WARNING,
                )
            else:
                command_service.add_log(
                    session,
                    command,
                    f"Опрос `/check-*` не удался ({exc.message}) — продолжаем опрос, "
                    "команду не переотправляем (защита от дубля)",
                    level=JobLogLevel.WARNING,
                )
            self._schedule_backoff(command.id, attempts=1)
            report.waiting += 1
            return

        attempts = command_service.register_attempt(session, command, error=exc.message)
        retry_max = max(1, self._setting(session, RETRY_MAX_SETTING))
        command_service.add_log(
            session,
            command,
            f"Попытка {attempts}/{retry_max} не удалась: {exc.message}",
            level=JobLogLevel.WARNING,
        )
        if attempts >= retry_max:
            self._finish(
                session,
                command,
                CommandStatus.ERROR,
                message=f"Задача не выполнена за {attempts} попыток: {exc.message}",
                report=report,
                level=JobLogLevel.ERROR,
            )
            return

        self._schedule_backoff(command.id, attempts)
        report.retried += 1
        report.waiting += 1

    # --- П-1/Д-1: push учётных данных 1С и списка оборудования ---------------

    async def _sync_pushes(
        self,
        session: Any,  # noqa: ANN401 - Session
        report: DispatchReport,
    ) -> None:
        """Дослать то, что на кассе разошлось: пароль 1С (П-1) и устройства (Д-1).

        Один `GET /health` отвечает на оба вопроса — агент докладывает
        `credentials_version` и `devices_version`, — поэтому проверка общая и
        выполняется не чаще `credentials_push_interval_sec`: трафик мониторинга
        не удваивается (§7.6).

        Расхождение означает переустановку агента, потерю конфига или изменение
        на сервере (смена пароля, правка списка оборудования) — во всех случаях
        сервер сам «лечит» кассу push-ом `POST /credentials` / `POST /devices/config`.
        """
        interval = max(1, self._setting(session, CREDENTIALS_INTERVAL_SETTING))
        window = self._setting(session, ONLINE_WINDOW_SETTING)
        now = utcnow()
        stores = list(session.scalars(select(Store).where(Store.is_active.is_(True))).all())
        alive: set[int] = set()

        for store in stores:
            alive.add(store.id)
            if not store.is_online(now, window):
                continue
            checked_at = self._credentials_checked.get(store.id)
            if checked_at is not None and (now - checked_at).total_seconds() < interval:
                continue
            self._credentials_checked[store.id] = now

            credentials_pushed = False
            devices_pushed = 0
            try:
                async with self._client(store) as client:
                    health = await client.get_health()
                    if health.credentials_version != store.credentials_version:
                        await client.push_credentials(
                            CredentialsPush(
                                user_name=store.user_name_1c,
                                password=self._decrypt_password(store),
                                credentials_version=store.credentials_version,
                            )
                        )
                        credentials_pushed = True
                    if health.devices_version != store.devices_version:
                        payload = device_service.push_payload(session, store)
                        await client.push_devices_config(payload)
                        devices_pushed = len(payload.devices)
            except AgentError as exc:
                logger.debug(
                    "Магазин id=%s: push отложен до следующей сверки (%s)",
                    store.id,
                    exc.message,
                )
                continue
            except Exception:  # noqa: BLE001 - одна касса не роняет цикл
                logger.exception("Магазин id=%s: сбой push учётных данных/устройств", store.id)
                continue

            if credentials_pushed:
                report.credentials_pushed += 1
                logger.info(
                    "Магазин id=%s: учётные данные 1С доставлены агенту (версия %s)",
                    store.id,
                    store.credentials_version,
                )
            if devices_pushed:
                report.devices_pushed += 1
                logger.info(
                    "Магазин id=%s: список оборудования доставлен агенту "
                    "(версия %s, устройств %s)",
                    store.id,
                    store.devices_version,
                    devices_pushed,
                )

        for store_id in list(self._credentials_checked):
            if store_id not in alive:
                del self._credentials_checked[store_id]

    def _decrypt_password(self, store: Store) -> str:
        """Расшифровать пароль 1С для push-а (П-1, §8.3).

        Значение живёт только внутри запроса и никогда не логируется (I-7).
        """
        if not store.password_enc or self._secret_box is None:
            return ""
        try:
            return self._secret_box.decrypt(store.password_enc) or ""
        except Exception:  # noqa: BLE001 - ключ сменили/испортили: не роняем цикл
            logger.warning(
                "Магазин id=%s: не удалось расшифровать пароль 1С (проверьте secrets.key)",
                store.id,
            )
            return ""

    # --- Завершение задачи --------------------------------------------------

    def _finish(  # noqa: PLR0913 - параметры цикла
        self,
        session: Any,  # noqa: ANN401 - Session
        command: Command,
        status: str | CommandStatus,
        *,
        message: str | None = None,
        log_text: str | None = None,
        report: DispatchReport | None = None,
        level: str | JobLogLevel = JobLogLevel.INFO,
    ) -> None:
        """Закрыть задачу: статус, сообщение, текст лога и счётчики отчёта."""
        command_service.set_status(session, command, status)
        if message:
            command_service.add_log(session, command, message, level=level)
            if str(status) != str(CommandStatus.SUCCESS):
                command.last_error = message[: command_service.MAX_LOG_LINE_CHARS]
        if log_text:
            command_service.save_excerpt(session, command, log_text)
            command_service.log_from_text(session, command, log_text, prefix="лог")
        session.flush()

        if report is None:
            return
        if str(status) == str(CommandStatus.SUCCESS):
            report.succeeded += 1
        else:
            report.failed += 1


def build_1c_payload(store: Store, command: Command) -> dict[str, Any]:
    """Собрать тело `/run-merge` или `/run-update-db` из магазина и задачи (§5.1.1).

    Реквизиты подключения берутся из карточки магазина (`cluster_name`,
    `database_name`, `version_1c`, `user_name_1c`) — админ их не повторяет.

    **Пароля здесь нет и быть не может (П-1, I-7):** сервер доставляет его
    отдельно push-ом `POST /credentials`, а агент подставляет из своего
    зашифрованного профиля (§5.1.1, п. 2 расширений).
    """
    params = command.params or {}
    payload: dict[str, Any] = {
        "server_name": store.cluster_name,
        "base_name": store.database_name,
        "version_1c": store.version_1c,
        "user_name": store.user_name_1c,
    }
    for key in (
        "file_name",
        "full_merge",
        "extension_name",
        "settings_file_name",
        "is_fast_update",
        "is_extension",
    ):
        if key in params and params[key] is not None:
            payload[key] = params[key]
    # FR-4/Д-16/Д-17: имя расширения может жить в карточке магазина. Тип операции
    # («основная/расширение») решает единый источник `is_extension_operation`:
    # явный флаг `is_extension` из параметров операции → расширение в имени файла
    # (`.cf`/`.cfe`) → `extension_name` (параметры или карточка). Если идёт на
    # расширение, подставляем имя из карточки, когда в параметрах его нет; если
    # на основную конфигурацию — расширение из карточки **не** подставляем, иначе
    # `.cf`-обновление на магазине с настроенным расширением превращалось бы в `.cfe`.
    # Флаг `is_extension` всегда передаём агенту явно, чтобы он не выводил тип из
    # карточки заново (иначе на агенте снова всплыл бы `-Extension`).
    file_name = str(params.get("file_name") or "")
    ext = payload.get("extension_name") or store.extension_name
    is_ext = is_extension_operation(params.get("is_extension"), file_name, ext)
    payload["is_extension"] = is_ext
    if is_ext:
        payload["extension_name"] = payload.get("extension_name") or store.extension_name
    else:
        payload.pop("extension_name", None)
    return payload


def describe_kind(kind: str) -> str:
    """Человеческое название операции для журнала задачи (§4.4)."""
    return {
        str(CommandKind.CHECK_FILES): "Проверка файлов",
        str(CommandKind.DOWNLOAD_FILE): "Скачивание файла",
        str(CommandKind.RUN_MERGE): "Слияние конфигурации",
        str(CommandKind.RUN_UPDATE_DB): "Обновление конфигурации БД",
        str(CommandKind.SEND_MESSAGE): "Сообщение пользователям",
        str(CommandKind.NOTIFY): "Уведомление",
        str(CommandKind.NOTIFY_CANCEL): "Отзыв уведомления",
    }.get(kind, kind)


