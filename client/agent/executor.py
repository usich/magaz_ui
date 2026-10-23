"""Исполнитель 1С-операций агента: очередь, мьютекс, состояние (§7.3).

Здесь живёт всё, что делает `merge` и `update-db` безопасными для кассы:

* **I-13** — операции никогда не идут параллельно: очередь + `asyncio.Lock`;
  второй запрос, пока идёт первый, сразу получает `409 Conflict` (§7.3, Q23).
* **I-8/FR-9** — работа идёт в фоновом воркере, HTTP отвечает `202` немедленно.
* **FR-6** — полный цикл при `is_fast_update=false`: завершение сеансов →
  `taskkill 1cv8.exe` → `/UpdateDBCfg` → разрешение работы пользователей.
* **§7.3** — состояние операции пишется в `agent-state.json`; на старте службы
  незавершённая операция помечается `interrupted` и доносится серверу в `/health`
  (админ видит, что касса перезагрузилась посреди обновления).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from client.agent import messenger, one_c
from client.agent.one_c import OneCPaths
from client.agent.processes import DEFAULT_PROVIDER
from shared.paths import is_extension_operation
from shared.schemas import BusyState
from shared.timeutil import to_iso_z, utcnow

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    from client.agent.processes import ProcessProvider
    from shared.schemas import MergeParams, UpdateParams

logger = logging.getLogger(__name__)

__all__ = [
    "OPERATION_MERGE",
    "OPERATION_UPDATE_DB",
    "STATE_FAILED",
    "STATE_FINISHED",
    "STATE_INTERRUPTED",
    "STATE_RUNNING",
    "OperationBusyError",
    "OperationRunner",
    "OperationState",
    "run_merge_process",
    "run_update_db_process",
]

#: Классы операций: ровно те, что последовательны на одной базе (I-13)
OPERATION_MERGE = "merge"
OPERATION_UPDATE_DB = "update-db"

#: Жизненный цикл операции в `agent-state.json` (§7.3)
STATE_RUNNING = "running"
STATE_FINISHED = "finished"
STATE_FAILED = "failed"
STATE_INTERRUPTED = "interrupted"

#: Сколько записей истории операций помним в памяти (для `/jobs` и диагностики)
HISTORY_LIMIT = 20


class OperationBusyError(RuntimeError):
    """На агенте уже идёт 1С-операция — второй запрос получает `409` (§7.3, I-13)."""

    def __init__(self, busy: BusyState, current: "OperationState | None" = None) -> None:
        self.busy = busy
        self.current = current
        kind = (current.operation if current else None) or "1С"
        super().__init__(f"На агенте уже выполняется операция {kind!r} — повторите позже")


@dataclass(slots=True)
class OperationState:
    """Состояние операции для `agent-state.json` и `/health` (§7.3)."""

    operation: str
    status: str
    log_file: str
    started_at: str
    finished_at: str | None = None
    file_name: str | None = None
    extension_name: str | None = None
    is_fast_update: bool | None = None
    message: str | None = None

    @property
    def is_active(self) -> bool:
        """Операция ещё выполняется (её нельзя считать завершённой)."""
        return self.status == STATE_RUNNING


# --- Выполнение операций (FR-6, FR-9) ---------------------------------------


async def run_merge_process(
    params: MergeParams,
    log_path: Path,
    paths: OneCPaths,
    provider: ProcessProvider | None = None,
) -> None:
    """Фоновое слияние конфигурации или расширения (перенос v2, FR-9)."""
    await one_c.run_1c_command(
        one_c.build_merge_command(params, log_path, paths), paths.log_executor, provider=provider
    )


async def run_update_db_process(
    params: UpdateParams,
    log_path: Path,
    paths: OneCPaths,
    provider: ProcessProvider | None = None,
) -> None:
    """Фоновое обновление конфигурации БД (Д-18, FR-6).

    1. если `is_fast_update = false`: завершение работы пользователей командой 1С
       `/C ЗавершитьРаботуПользователей` — 1С сама закрывает сеансы (без taskkill);
    2. `/UpdateDBCfg` (устанавливает код разрешения);
    3. если `is_fast_update = false`: снятие блокировки базы и разрешение работы
       пользователей командой `/C РазрешитьРаботуПользователей`.
    """
    if not params.is_fast_update:
        await one_c.clear_log_file(paths.log_shutdown_users)
        await one_c.run_1c_command(
            one_c.build_shutdown_users_command(params, paths.log_shutdown_users, paths),
            paths.log_executor,
            timeout=one_c.USERS_SHUTDOWN_TIMEOUT,
            provider=provider,
        )

    code = await one_c.run_1c_command(
        one_c.build_update_db_command(params, log_path, paths),
        paths.log_executor,
        provider=provider,
    )
    # FR-5: при `/UpdateDBCfg` 1С может завершиться успешно, но не записать текст
    # в `/Out` — файл остаётся пустым (или с одним BOM-заголовком), и
    # `/check-update-db` вечно возвращает `202` (наблюдение из эксплуатации:
    # обновление прошло, а лог пуст). Статус решает факт завершения процесса +
    # пустой лог, а **не код возврата** (I-4): платформа `CONFIG /UpdateDBCfg`
    # при успехе может вернуть ненулевой код. Поэтому если процесс вышел
    # (`code is not None`, не таймаут) и лог эффективно пуст (включая только BOM) —
    # фиксируем успех сами, чтобы сервер закрыл задачу, а не держал её «в работе»
    # и не счёл «запуск не состоялся».
    if code is not None and log_path.exists():
        text = log_path.read_text(encoding="utf-8", errors="ignore")
        if one_c.log_text_is_effectively_empty(text):
            log_path.write_text(one_c.SUCCESS_UPDATE_MSG, encoding="utf-8")

    if not params.is_fast_update:
        await one_c.clear_log_file(paths.log_unlock_users)
        await one_c.run_1c_command(
            one_c.build_enable_users_command(params, paths.log_unlock_users, paths),
            paths.log_executor,
            timeout=one_c.USERS_UNLOCK_TIMEOUT,
            provider=provider,
        )


@dataclass(slots=True)
class _Job:
    """Задача в очереди исполнителя: что запускать и куда писать результат."""

    operation: str
    log_path: Path
    params: MergeParams | UpdateParams


class OperationRunner:
    """Очередь и мьютекс 1С-операций агента (§7.3, I-13, FR-9).

    Пока идёт (или ждёт своей очереди) хоть одна операция, агент занят: новый
    `/run-merge` или `/run-update-db` сразу получает `409 Conflict` — на одной
    базе они недопустимы параллельно. Факт запуска и его итог пишутся в
    `agent-state.json`, чтобы после перезагрузки кассы админ видел правду.
    """

    def __init__(
        self,
        *,
        paths: OneCPaths,
        state_path: Path,
        provider: ProcessProvider = DEFAULT_PROVIDER,
    ) -> None:
        self.paths = paths
        self.state_path = state_path
        self.provider = provider
        self._queue: asyncio.Queue[_Job] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._worker: asyncio.Task[None] | None = None
        self._current: _Job | None = None
        self._active_operation: str | None = None
        self._state: OperationState | None = None
        self._interrupted: str | None = None
        self._history: list[OperationState] = []

    # --- Что агент показывает наружу (§5.1.2 `/health`) ----------------------
    @property
    def busy_state(self) -> BusyState:
        """Занятость 1С-операциями для `/health` (Q23): что идёт прямо сейчас."""
        return BusyState(
            merge=self._active_operation == OPERATION_MERGE,
            update=self._active_operation == OPERATION_UPDATE_DB,
        )

    @property
    def active_operation(self) -> str | None:
        """Класс выполняемой/ожидающей операции либо `None`, если агент свободен."""
        return self._active_operation

    @property
    def pending(self) -> int:
        """Сколько задач ждёт в очереди (для диагностики и тестов)."""
        return self._queue.qsize()

    @property
    def current_state(self) -> OperationState | None:
        """Последняя запись состояния операции (`agent-state.json`)."""
        return self._state

    @property
    def interrupted_operation(self) -> str | None:
        """Операция, прерванная перезагрузкой кассы: уходит в `/health` (§7.3)."""
        return self._interrupted

    @property
    def history(self) -> tuple[OperationState, ...]:
        """История операций текущего запуска службы (новые — в конце)."""
        return tuple(self._history)

    # --- Файл состояния `agent-state.json` (§7.3) ----------------------------
    def mark_interrupted(self) -> OperationState | None:
        """Старт службы: незавершённую операцию помечаем `interrupted` (§7.3).

        Так админ видит, что касса перезагрузилась посреди обновления, а не
        «всё прошло хорошо».
        """
        state = self._read_state()
        if state is None:
            return None

        self._state = state
        if state.status == STATE_RUNNING:
            state.status = STATE_INTERRUPTED
            state.finished_at = to_iso_z(utcnow())
            state.message = "Служба агента перезапущена во время операции"
            self._interrupted = state.operation
            self._history.append(state)
            self._write_state()
            logger.warning(
                "Незавершённая операция %s (лог %s) помечена interrupted",
                state.operation,
                state.log_file,
            )
        return state

    def _read_state(self) -> OperationState | None:
        """Прочитать состояние операции; битый файл — не повод падать."""
        if not self.state_path.exists():
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return OperationState(**data)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Файл состояния %s не читается (%s)", self.state_path, exc)
            return None

    def _write_state(self) -> None:
        """Записать состояние атомарно: файл не бывает «полузаписанным»."""
        if self._state is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_name(self.state_path.name + ".tmp")
            payload = json.dumps(asdict(self._state), ensure_ascii=False, indent=2)
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self.state_path)
        except OSError as exc:  # pragma: no cover - нет прав на запись
            logger.warning("Не удалось записать %s (%s)", self.state_path, exc)

    # --- Постановка операций (§5.1.1 `/run-*`) -------------------------------
    async def submit_merge(self, params: MergeParams) -> OperationState:
        """Принять `POST /run-merge`: обнулить лог и поставить задачу в очередь.

        Лог обнуляется до возврата `202` (I-3), поэтому «залипший успех»
        предыдущего запуска не может быть принят за результат новой операции.
        Лог-файл выбирается по типу слияния (`.cf`/`.cfe` из имени файла, Д-16),
        а не просто по наличию `extension_name`.
        """
        extension = is_extension_operation(
            params.is_extension, params.file_name, params.extension_name
        )
        log_path = self.paths.merge_log(params.extension_name if extension else None)
        await one_c.clear_log_file(log_path)
        return await self._enqueue(
            operation=OPERATION_MERGE,
            log_path=log_path,
            params=params,
            file_name=params.file_name,
            extension_name=params.extension_name,
        )

    async def submit_update_db(self, params: UpdateParams) -> OperationState:
        """Принять `POST /run-update-db`: обнулить лог и поставить задачу в очередь.

        Лог-файл выбирается по типу обновления (основная/расширение, Д-17), а не
        просто по наличию `extension_name`.
        """
        extension = is_extension_operation(params.is_extension, "", params.extension_name)
        log_path = self.paths.update_log(params.extension_name if extension else None)
        await one_c.clear_log_file(log_path)
        return await self._enqueue(
            operation=OPERATION_UPDATE_DB,
            log_path=log_path,
            params=params,
            extension_name=params.extension_name,
            is_fast_update=params.is_fast_update,
        )

    async def _enqueue(
        self,
        *,
        operation: str,
        log_path: Path,
        params: MergeParams | UpdateParams,
        file_name: str | None = None,
        extension_name: str | None = None,
        is_fast_update: bool | None = None,
    ) -> OperationState:
        """Поставить задачу в очередь или отказать с `409` (§7.3, Q23).

        Отказ получает **любая** вторая 1С-операция, а не только «того же класса»:
        параллельное обновление и слияние на одной базе недопустимы (I-13).
        """
        if self._active_operation is not None:
            raise OperationBusyError(self.busy_state, self._state)

        state = OperationState(
            operation=operation,
            status=STATE_RUNNING,
            log_file=str(log_path),
            started_at=to_iso_z(utcnow()) or "",
            file_name=file_name,
            extension_name=extension_name,
            is_fast_update=is_fast_update,
        )
        self._state = state
        # Новая операция заменяет отчёт о прерванной перезагрузкой (§7.3)
        self._interrupted = None
        self._active_operation = operation
        self._write_state()
        self._queue.put_nowait(_Job(operation=operation, log_path=log_path, params=params))
        self._ensure_worker()
        logger.info("Операция %s поставлена в очередь (лог %s)", operation, log_path)
        return state

    # --- Фоновый воркер (I-8, FR-9) ------------------------------------------
    def _ensure_worker(self) -> asyncio.Task[None]:
        """Лениво поднять воркера: HTTP-слой не ждёт 1С (I-8, I-10)."""
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(
                self._worker_loop(), name="agent-operations"
            )
        return self._worker

    async def _worker_loop(self) -> None:
        """Разбирать очередь операций по одной."""
        while True:
            job = await self._queue.get()
            try:
                await self._run_job(job)
            finally:
                self._queue.task_done()

    async def _run_job(self, job: _Job) -> None:
        """Выполнить операцию под мьютексом (I-13)."""
        async with self._lock:
            self._current = job
            try:
                if job.operation == OPERATION_MERGE:
                    await run_merge_process(job.params, job.log_path, self.paths, self.provider)
                else:
                    await run_update_db_process(job.params, job.log_path, self.paths, self.provider)
            except Exception as exc:  # служба не должна падать из-за 1С
                logger.exception("Операция %s завершилась ошибкой", job.operation)
                self._finish(STATE_FAILED, message=str(exc))
            else:
                self._finish(STATE_FINISHED)
            finally:
                self._current = None
                self._active_operation = None

    def _finish(self, status: str, *, message: str | None = None) -> None:
        """Закрыть операцию: статус, время окончания и история (§7.3)."""
        if self._state is None:
            return
        self._state.status = status
        self._state.finished_at = to_iso_z(utcnow())
        if message:
            self._state.message = message
        self._history.append(self._state)
        del self._history[:-HISTORY_LIMIT]
        self._write_state()
        logger.info("Операция %s: %s", self._state.operation, status)

    async def wait_idle(self, timeout: float = 60.0) -> None:
        """Дождаться, пока очередь опустеет (остановка службы и тесты)."""
        if self._worker is None:
            return
        await asyncio.wait_for(self._queue.join(), timeout=timeout)

    async def shutdown(self) -> None:
        """Остановить воркер: незавершённая операция останется `running` в состоянии.

        На следующем старте `mark_interrupted()` честно превратит её в
        `interrupted` (§7.3) — именно этого и ждёт админ после перезагрузки кассы.
        """
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                logger.info("Воркер операций остановлен")
        self._worker = None


