"""Задачи и журнал задач (§4.3, §4.4, §11.4, §11.6) — сервисный слой.

Все операции, которые сервер ставит на кассу, живут строкой в `commands`:
кто поставил, что просил, чем закончилось. Дополнительно к строке идёт журнал
`job_logs` — то, что видит админ на странице `/jobs/{id}` (§11.6).

Инварианты, за которые отвечает модуль:

* **I-7** — в `commands.params` не бывает паролей: `sanitize_params()`
  выбрасывает ключи `password`/`token`/`secret` (рекурсивно), поэтому значение
  никогда не попадёт ни в БД, ни в JSON-ответ, ни в HTML;
* **§4.3** — обрезка `log_excerpt` (`log_excerpt_limit_bytes`, по умолчанию
  200 КБ): храним **хвост** лога, потому что итог операции 1С пишет в конец;
* **§4.3** — `finished_at` ставится только у завершающих статусов, а
  `started_at` — при первой попытке отправки, поэтому в UI видно и «ещё не
  началось», и «идёт N секунд»;
* **R12/FR-28** — запуск обновления отклоняется, если ту же серверную ИБ
  (`cluster_name` + `database_name`) уже обновляет другой магазин или у самого
  магазина висит незавершённая задача обновления. Проверка
  (`find_update_conflicts`) и постановка задач идут в одном критическом участке
  (`dispatch_guard`), поэтому два администратора, нажавшие кнопку одновременно,
  не «проскочат» запрет.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from server.db.models import Command, JobLog, Store
from shared.enums import CommandKind, CommandStatus, JobLogLevel
from shared.timeutil import utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "ACTIVE_STATUSES",
    "FINISHED_STATUSES",
    "KIND_LABELS",
    "LOCAL_UPDATE_KIND",
    "UPDATE_KINDS",
    "UpdateConflict",
    "add_log",
    "count_active",
    "counts_by_status",
    "create_command",
    "create_local_command",
    "delete_command",
    "delete_commands",
    "dispatch_guard",
    "find_local_update_conflict",
    "find_update_conflicts",
    "get_command",
    "list_commands",
    "list_pending",
    "list_pending_local",
    "log_from_text",
    "register_attempt",
    "sanitize_params",
    "save_excerpt",
    "set_status",
    "status_age_sec",
]

#: Статусы, в которых задача ещё не завершена (плитка «Задач в работе», §11.1)
ACTIVE_STATUSES: tuple[str, ...] = (
    CommandStatus.QUEUED,
    CommandStatus.SENT,
    CommandStatus.RUNNING,
)

#: Завершающие статусы: `finished_at` проставлен, задача в истории
FINISHED_STATUSES: tuple[str, ...] = (
    CommandStatus.SUCCESS,
    CommandStatus.FAILED,
    CommandStatus.TIMEOUT,
    CommandStatus.ERROR,
    CommandStatus.CANCELED,
)

#: Ключи, значения которых не имеют права попасть в `commands.params` (I-7)
SECRET_KEYS: frozenset[str] = frozenset(
    {"password", "password_enc", "token", "agent_token", "secret", "secret_key"}
)

#: По умолчанию `log_excerpt` режется до 200 КБ (§4.9 `log_excerpt_limit_bytes`)
DEFAULT_EXCERPT_LIMIT = 200 * 1024

#: Сколько строк лога максимум раскладываем в `job_logs` за один раз
MAX_LOG_LINES = 200
#: Ограничение длины одной строки журнала (защита от «полотна» из лога 1С)
MAX_LOG_LINE_CHARS = 4000

#: Маркеры ошибки в логе 1С (Приложение B: `Ошибка`, `Error`)
ERROR_MARKERS = ("Ошибка", "ошибка", "Error", "error", "FAILED")

#: Виды задач, которые меняют серверную ИБ 1С: их параллельный запуск запрещён (R12)
UPDATE_KINDS: tuple[str, ...] = (str(CommandKind.RUN_MERGE), str(CommandKind.RUN_UPDATE_DB))

#: Вид задачи обновления **локальной** базы (Д-24): свой запрет R12, см.
#: `find_local_update_conflict()` — одну локальную базу обновляет одна задача.
LOCAL_UPDATE_KIND: str = str(CommandKind.RUN_UPDATE_DB_LOCAL)

#: Человекочитаемые названия операций обновления — для текстов конфликтов R12
KIND_LABELS: dict[str, str] = {
    str(CommandKind.RUN_MERGE): "слияние конфигурации",
    str(CommandKind.RUN_UPDATE_DB): "обновление конфигурации БД",
    LOCAL_UPDATE_KIND: "обновление локальной базы",
}

#: Замок критического участка «проверка R12 → создание задач» (FR-28, §14.1)
_DISPATCH_LOCK = threading.RLock()


def sanitize_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Вернуть копию параметров команды без секретов (I-7).

    Рекурсивно: словари внутри `params` тоже чистятся, потому что тело запроса
    к агенту может содержать вложенный профиль 1С.
    """
    if not params:
        return {}

    clean: dict[str, Any] = {}
    for key, value in params.items():
        if str(key).casefold() in SECRET_KEYS:
            continue
        clean[key] = sanitize_params(value) if isinstance(value, dict) else value
    return clean


def _excerpt_limit(session: Session | None, fallback: int = DEFAULT_EXCERPT_LIMIT) -> int:
    """Предел обрезки лога из настроек §4.9 (при отсутствии — значение по умолчанию)."""
    if session is None:
        return fallback
    try:
        from server.services import settings_service

        return max(1024, settings_service.get_int(session, "log_excerpt_limit_bytes"))
    except Exception:  # noqa: BLE001 - настройка не критична для сохранения лога
        return fallback


def save_excerpt(session: Session | None, command: Command, text: str | None) -> None:
    """Сохранить хвост лога в `commands.log_excerpt` с обрезкой (§4.3)."""
    if text is None:
        return
    limit = _excerpt_limit(session)
    command.log_excerpt = text if len(text) <= limit else text[-limit:]


def guess_level(line: str) -> str:
    """Уровень строки журнала по её тексту (§4.4).

    Отдельного источника уровней у `/check-*` нет (перенос v2 отдаёт один
    текстовый лог), поэтому ошибки определяем по маркерам Приложения B.
    """
    for marker in ERROR_MARKERS:
        if marker in line:
            return JobLogLevel.ERROR
    return JobLogLevel.INFO

def add_log(
    session: Session,
    command: Command,
    message: str | None,
    *,
    level: str | JobLogLevel = JobLogLevel.INFO,
) -> JobLog | None:
    """Добавить строку журнала задачи (§4.4). Пустые строки не пишем."""
    if message is None:
        return None
    text = str(message).strip()
    if not text:
        return None
    if len(text) > MAX_LOG_LINE_CHARS:
        text = text[:MAX_LOG_LINE_CHARS]

    entry = JobLog(command_id=command.id, level=str(level), message=text)
    session.add(entry)
    session.flush()
    return entry


def log_from_text(
    session: Session,
    command: Command,
    text: str | None,
    *,
    prefix: str | None = None,
) -> int:
    """Разложить текстовый лог `/check-*` в строки `job_logs` (§4.4).

    Хранится не больше `MAX_LOG_LINES` последних строк: лог 1С может содержать
    десятки тысяч строк, а странице задач нужен читаемый хвост.
    """
    if not text:
        return 0
    lines = [line.strip() for line in str(text).splitlines()]
    lines = [line for line in lines if line][-MAX_LOG_LINES:]
    for line in lines:
        add_log(session, command, f"{prefix}: {line}" if prefix else line, level=guess_level(line))
    return len(lines)


def create_command(
    session: Session,
    *,
    store_id: int,
    kind: str | CommandKind,
    params: dict[str, Any] | None = None,
    created_by: str | None = None,
) -> Command:
    """Создать задачу в статусе `queued` (§4.3): её подхватит диспетчер.

    Параметры проходят через `sanitize_params()` — пароль 1С в `commands.params`
    не попадает никогда (I-7).
    """
    command = Command(
        store_id=store_id,
        kind=str(kind),
        params=sanitize_params(params),
        status=CommandStatus.QUEUED,
        attempts=0,
        created_by=created_by,
    )
    session.add(command)
    session.flush()
    logger.info(
        "Задача id=%s создана: магазин %s, тип %s (автор %s)",
        command.id,
        store_id,
        command.kind,
        created_by or "—",
    )
    return command


def create_local_command(
    session: Session,
    *,
    local_base_id: int,
    kind: str | CommandKind,
    params: dict[str, Any] | None = None,
    created_by: str | None = None,
) -> Command:
    """Создать задачу обновления **локальной** базы (Д-24) в статусе `queued`.

    Локальную задачу подхватит `LocalUpdateMonitor`, а не диспетчер: она не
    отправляется агенту, а выполняется `1cv8.exe` напрямую на этой машине.
    Параметры проходят через `sanitize_params()` — пароль 1С не попадает в
    `commands.params` (I-7); подключение берётся из карточки базы.
    """
    command = Command(
        store_id=None,
        local_base_id=local_base_id,
        kind=str(kind),
        params=sanitize_params(params),
        status=CommandStatus.QUEUED,
        attempts=0,
        created_by=created_by,
    )
    session.add(command)
    session.flush()
    logger.info(
        "Задача id=%s создана: локальная база %s, тип %s (автор %s)",
        command.id,
        local_base_id,
        command.kind,
        created_by or "—",
    )
    return command


def get_command(session: Session, command_id: int) -> Command | None:
    """Задача по id вместе с магазином/локальной базой и журналом (§11.6)."""
    stmt = (
        select(Command)
        .where(Command.id == command_id)
        .options(selectinload(Command.store), selectinload(Command.local_base), selectinload(Command.logs))
    )
    return session.scalars(stmt).one_or_none()


def delete_command(session: Session, command_id: int) -> Command | None:
    """Жёстко удалить задачу вместе с её журналом (Э10, §11.6).

    Каскад `job_logs` уходит через relationship `Command.logs`
    (`cascade="all, delete-orphan"`) и FK `ondelete="CASCADE"`. Задача в любом
    статусе может быть удалена: для незавершённой это «снять с очереди» —
    диспетчер (который читает `list_pending` из той же таблицы) перестанет её
    видеть сразу после коммита. Возвращает `None`, если задачи нет.
    """
    command = session.get(Command, command_id)
    if command is None:
        return None
    session.delete(command)
    session.flush()
    logger.info("Задача id=%s удалена (журнал ушёл вместе с ней)", command_id)
    return command


def delete_commands(session: Session, command_ids: Sequence[int]) -> int:
    """Жёстко удалить несколько задач; возвращает число удалённых (§11.6, Э10).

    Несуществующие id пропускаются (идемпотентность массовой операции), поэтому
    повторный запрос с тем же списком не падает. Каждая задача удаляется через
    ORM-каскад журнала `job_logs`.
    """
    ids = [int(item) for item in command_ids if item]
    if not ids:
        return 0
    deleted = 0
    for command_id in ids:
        if delete_command(session, command_id) is not None:
            deleted += 1
    session.flush()
    logger.info("Удалено задач: %s (из %s запрошенных)", deleted, len(ids))
    return deleted


def list_commands(
    session: Session,
    *,
    store_id: int | None = None,
    local_base_id: int | None = None,
    kind: str | None = None,
    status: str | None = None,
    active_only: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[Command]:
    """История задач для страницы `/jobs` (§11.6), новые — первыми.

    `store_id` и `local_base_id` взаимоисключающие (Д-24): задача магазина
    ссылается на магазин, локальная — на базу. Оба не заданы — все задачи.
    """
    stmt = (
        select(Command)
        .options(selectinload(Command.store), selectinload(Command.local_base))
        .order_by(Command.id.desc())
    )
    if store_id is not None:
        stmt = stmt.where(Command.store_id == store_id)
    if local_base_id is not None:
        stmt = stmt.where(Command.local_base_id == local_base_id)
    if kind:
        stmt = stmt.where(Command.kind == kind)
    if status:
        stmt = stmt.where(Command.status == status)
    if active_only:
        stmt = stmt.where(Command.status.in_(ACTIVE_STATUSES))
    return list(session.scalars(stmt.limit(max(1, limit)).offset(max(0, offset))).all())


def list_pending(session: Session) -> list[Command]:
    """Незавершённые задачи магазинов — вход диспетчера (§3.3, FR-24).

    Дополнительно к «в работе» берём задачи в статусе `queued`: после
    перезапуска сервера очередь не теряется, а команды, не дошедшие до кассы,
    отправляются снова. **Локальные** задачи (по базе на этой машине, Д-24)
    отсюда исключены — их выполняет `LocalUpdateMonitor` (`list_pending_local`).
    """
    stmt = (
        select(Command)
        .where(
            Command.status.in_(ACTIVE_STATUSES),
            Command.store_id.is_not(None),
            Command.local_base_id.is_(None),
        )
        .options(selectinload(Command.store))
        .order_by(Command.id)
    )
    return list(session.scalars(stmt).all())


def list_pending_local(session: Session) -> list[Command]:
    """Незавершённые задачи **локальных** баз — вход LocalUpdateMonitor (Д-24).

    Это задачи с `local_base_id`, но без `store_id`: их не видит диспетчер
    магазинов. `queued`/`running` — «в работе»; `running` встречается после
    перезапуска сервера посреди операции — монитор продолжает выполнение.
    """
    stmt = (
        select(Command)
        .where(
            Command.status.in_(ACTIVE_STATUSES),
            Command.store_id.is_(None),
            Command.local_base_id.is_not(None),
        )
        .options(selectinload(Command.local_base))
        .order_by(Command.id)
    )
    return list(session.scalars(stmt).all())


def count_active(session: Session) -> int:
    """Сколько задач ещё не завершено (плитка «Задач в работе», §11.1)."""
    stmt = select(func.count(Command.id)).where(Command.status.in_(ACTIVE_STATUSES))
    return int(session.scalar(stmt) or 0)


def counts_by_status(
    session: Session, *, store_id: int | None = None, local_base_id: int | None = None
) -> dict[str, int]:
    """Счётчики задач по статусам — фильтры страницы `/jobs` (§11.6)."""
    stmt = select(Command.status, func.count(Command.id)).group_by(Command.status)
    if store_id is not None:
        stmt = stmt.where(Command.store_id == store_id)
    if local_base_id is not None:
        stmt = stmt.where(Command.local_base_id == local_base_id)
    return {str(status): int(count) for status, count in session.execute(stmt).all()}


def register_attempt(session: Session, command: Command, *, error: str | None = None) -> int:
    """Учесть попытку отправки (§4.3 `attempts`, §4.9 `command_retry_max`)."""
    command.attempts = int(command.attempts or 0) + 1
    if error:
        command.last_error = error[:MAX_LOG_LINE_CHARS]
    command.started_at = command.started_at or utcnow()
    session.flush()
    return command.attempts


def set_status(session: Session, command: Command, status: str | CommandStatus) -> Command:
    """Сменить статус задачи, аккуратно заполняя `started_at`/`finished_at` (§4.3)."""
    new_status = str(status)
    if new_status != command.status:
        logger.info("Задача id=%s: %s → %s", command.id, command.status, new_status)
    command.status = new_status
    if new_status in (CommandStatus.SENT, CommandStatus.RUNNING):
        command.started_at = command.started_at or utcnow()
    if new_status in FINISHED_STATUSES:
        command.finished_at = command.finished_at or utcnow()
    session.flush()
    return command


def status_age_sec(command: Command, *, now: Any = None) -> float:  # noqa: ANN401 - datetime | None
    """Сколько секунд задача «висит» от первой отправки (§11.4: таймаут).

    Отсчёт идёт от `started_at` (первая отправка), иначе — от `created_at`:
    задача, которая ждёт появления кассы в сети, тоже должна иметь предел.
    """
    reference = command.started_at or command.created_at
    if reference is None:
        return 0.0
    moment = now or utcnow()
    return max(0.0, (moment - reference).total_seconds())


# --- Запрет одновременного обновления одной ИБ (R12, FR-28, П-5) --------------


@dataclass(frozen=True)
class UpdateConflict:
    """Причина, по которой запуск обновления запрещён (R12, FR-28, П-5).

    Три случая одной структурой: `same_store` — у магазина уже есть незавершённая
    задача обновления («одного `ip`» из ответа заказчика), `same_database` — ИБ
    магазина прямо сейчас обновляет другой магазин, `same_database_batch` — в
    одном запуске два магазина одной серверной ИБ.
    """

    reason: str
    store_ids: tuple[int, ...]
    store_names: tuple[str, ...]
    cluster_name: str | None = None
    database_name: str | None = None
    job_id: int | None = None
    job_kind: str | None = None
    busy_store_name: str | None = None

    @property
    def stores_text(self) -> str:
        """Магазины в тексте ответа: «Магазин №5», «Магазин №6»."""
        return ", ".join(f"«{name}»" for name in self.store_names)

    @property
    def database_text(self) -> str:
        """Серверная ИБ в терминах 1С: `cluster_name\\database_name` (§2 Q2)."""
        return f"{self.cluster_name or '—'}\\{self.database_name or '—'}"

    @property
    def message(self) -> str:
        """Готовый текст конфликта для `detail` ответа `409` (§5.2.2)."""
        if self.reason == "same_store":
            label = KIND_LABELS.get(str(self.job_kind), "обновление")
            return (
                f"магазин {self.stores_text} уже выполняет операцию «{label}» "
                f"(задача #{self.job_id}): дождитесь её завершения и запустите снова"
            )
        if self.reason == "same_database":
            return (
                f"серверную ИБ «{self.database_text}» уже обновляет магазин "
                f"«{self.busy_store_name}» (задача #{self.job_id}): исключите магазин "
                f"{self.stores_text} из запуска или дождитесь завершения"
            )
        return (
            f"магазины {self.stores_text} работают с одной серверной ИБ "
            f"«{self.database_text}»: одновременное обновление запрещено — "
            "исключите один из магазинов из запуска и повторите"
        )


def _ib_key(store: Store) -> tuple[str, str] | None:
    """Ключ серверной ИБ магазина: `cluster_name` + `database_name` (§2 Q2).

    Регистр и лишние пробелы не важны: `srv-1c` и ` SRV-1C ` — одна ИБ. `None`
    означает «ИБ не определена» (одно из полей пустое): по такой паре запрет R12
    не применяется, иначе магазины без заполненной базы блокировали бы друг друга.
    """
    cluster = (store.cluster_name or "").strip().casefold()
    database = (store.database_name or "").strip().casefold()
    if not cluster or not database:
        return None
    return cluster, database


def active_update_commands(
    session: Session, *, kinds: Sequence[str] = UPDATE_KINDS
) -> list[Command]:
    """Незавершённые задачи, меняющие серверную ИБ, — вход проверки R12.

    Один запрос на весь запуск: задач «в работе» мало даже при парке 35–40
    магазинов (§2 Q7). Карточка магазина нужна для его ИБ, поэтому `selectinload`.
    """
    stmt = (
        select(Command)
        .where(Command.kind.in_(list(kinds)), Command.status.in_(ACTIVE_STATUSES))
        .options(selectinload(Command.store))
        .order_by(Command.id)
    )
    commands = list(session.scalars(stmt).all())
    logger.debug("Незавершённых задач обновления: %s", len(commands))
    return commands


def find_update_conflicts(
    session: Session,
    stores: Sequence[Store],
    *,
    kinds: Sequence[str] = UPDATE_KINDS,
) -> list[UpdateConflict]:
    """Проверить запрет одновременного обновления одной ИБ (R12, FR-28, П-5).

    Запрет жёсткий: любая найденная строка — основание отдать `409` и **не**
    создавать задачи. Отключённые магазины не рассматриваются: по ним действие
    всё равно пропускается (§11.4), «занимать ИБ» они не могут.
    """
    targets = [store for store in stores if store.is_active]
    if not targets:
        return []

    busy_by_store: dict[int, Command] = {}
    busy_by_database: dict[tuple[str, str], Command] = {}
    for command in active_update_commands(session, kinds=kinds):
        busy_by_store.setdefault(int(command.store_id), command)
        store_row = command.store
        key = _ib_key(store_row) if store_row is not None else None
        if key is not None:
            busy_by_database.setdefault(key, command)

    conflicts: list[UpdateConflict] = []
    groups: dict[tuple[str, str], list[Store]] = {}
    for store in targets:
        key = _ib_key(store)
        if key is not None:
            groups.setdefault(key, []).append(store)

        busy = busy_by_store.get(int(store.id))
        if busy is not None:
            conflicts.append(
                UpdateConflict(
                    reason="same_store",
                    store_ids=(int(store.id),),
                    store_names=(store.name,),
                    cluster_name=store.cluster_name,
                    database_name=store.database_name,
                    job_id=int(busy.id),
                    job_kind=str(busy.kind),
                )
            )
            continue

        busy_other = busy_by_database.get(key) if key is not None else None
        if busy_other is not None and int(busy_other.store_id) != int(store.id):
            other = busy_other.store
            conflicts.append(
                UpdateConflict(
                    reason="same_database",
                    store_ids=(int(store.id),),
                    store_names=(store.name,),
                    cluster_name=store.cluster_name,
                    database_name=store.database_name,
                    job_id=int(busy_other.id),
                    job_kind=str(busy_other.kind),
                    busy_store_name=getattr(other, "name", None) or f"id={busy_other.store_id}",
                )
            )

    for key, group in groups.items():
        # Если ИБ уже занята, точнее сообщить «её обновляет магазин X» (выше),
        # чем перечислять пару магазинов одного запуска.
        if len(group) < 2 or key in busy_by_database:
            continue
        conflicts.append(
            UpdateConflict(
                reason="same_database_batch",
                store_ids=tuple(int(item.id) for item in group),
                store_names=tuple(item.name for item in group),
                cluster_name=group[0].cluster_name,
                database_name=group[0].database_name,
            )
        )

    conflicts.sort(key=lambda item: (item.reason, item.store_ids))
    return conflicts


@contextmanager
def dispatch_guard() -> Iterator[None]:
    """Держать проверку R12 и постановку задач в одном критическом участке.

    Запрет «одна ИБ — одно обновление» нельзя проверить «в вакууме»: между
    проверкой и `INSERT` два администратора могли бы проскочить оба. Сервер
    работает одним процессом uvicorn (§14.1), а админские эндпоинты объявлены
    синхронными (`def`), то есть выполняются в threadpool FastAPI, — поэтому
    обычного потокового замка достаточно, и он не блокирует event loop.
    """
    with _DISPATCH_LOCK:
        yield


def find_local_update_conflict(session: Session, local_base_id: int) -> Command | None:
    """Вернуть активную задачу обновления той же **локальной** базы, если есть (Д-24).

    Аналог R12 для магазинов, но без «общей серверной ИБ»: локальная база — одна
    на машину, поэтому достаточно запрета «одна база — одно обновление». Активной
    считается задача `run_update_db_local` в статусе `queued`/`running`. Вызывается
    под `dispatch_guard()`, чтобы два администратора не проскочили запрет.
    """
    stmt = (
        select(Command)
        .where(
            Command.local_base_id == local_base_id,
            Command.kind == LOCAL_UPDATE_KIND,
            Command.status.in_(ACTIVE_STATUSES),
        )
        .order_by(Command.id)
        .limit(1)
    )
    return session.scalar(stmt)

