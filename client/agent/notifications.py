"""Уведомления на кассе: очередь, сессии и подтверждения (§7.4, ФН3, П-2).

Служба получает уведомление push-ом `POST /notify`, кладёт его в файл
`C:\\MagazAgent\\notifications.json` и отдаёт TrayAgent-у только на `127.0.0.1`.
TrayAgent **не ходит в сеть**: он знает лишь `127.0.0.1:8383`, поэтому токен
агента в пользовательскую сессию не попадает (§7.4). Учёт — по сессиям Windows
(П-6: справочника сотрудников нет): у каждого вошедшего пользователя своё окно и
своя строка отчёта `notification_receipts` (§4.8, Q50 — 1–2 сессии на ПК).

Кнопки окна (П-2):

* **«Ознакомлен»** → `POST /notifications/{id}/ack`: статус `acknowledged`;
* **«Отложить»** (Д-10) → `POST /notifications/{id}/snooze`: статус `snoozed`,
  `snooze_count += 1`, `snooze_until = сейчас + snooze_min`. Интервал задаёт
  **сервер** (`notification_snooze_min`, §4.9) и присылает его вместе с
  уведомлением — на кассе настройку не правят.

Отложенное **не считается ознакомленным**: оно уходит из видимости и снова
попадает в `GET /notifications/pending`, когда `snooze_until` прошёл. `info` сам
не всплывает — висит в трее до явного «Ознакомлен» (§7.4).

Подтверждения уходят на сервер пакетом `POST /api/v1/agent/receipts` вместе с
составом сессий: сервер по нему создаёт строки «ожидание», поэтому «X из Y»
честно показывает «продавец2 — ожидание» (§4.8). Если касса сервер не видит,
пакет остаётся на диске (`reported=false`) и досылается следующим циклом, в том
числе после перезапуска службы (§7.4).

`msg.exe */localhost` для `important`/`critical` (если `msg_enabled`) —
дублирующий канал службы для `important`/`critical`: он сработает, даже если
TrayAgent в сессии не запущен (§7.4).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from client.agent import messenger
from client.agent.config import AgentConfig
from client.agent.processes import DEFAULT_PROVIDER, ProcessProvider
from shared.enums import NotificationSeverity, ReceiptStatus
from shared.richtext import BODY_FORMAT_HTML, BODY_FORMAT_PLAIN, is_safe_image_name
from shared.schemas import (
    AgentSession,
    NotificationPendingItem,
    NotifyRequest,
    ReceiptBatch,
    ReceiptItem,
    SessionRef,
)
from shared.timeutil import parse_iso_z, to_iso_z, utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "ARCHIVE_LIMIT",
    "DEFAULT_SNOOZE_MIN",
    "IMAGE_API_PATH",
    "IMAGE_CACHE_DIR_NAME",
    "IMAGE_MEDIA_TYPES",
    "MAX_QUEUE",
    "MSG_SEVERITIES",
    "RECEIPTS_STATUS_PATH",
    "RETRY_DELAY_SEC",
    "SESSION_STALE_HOURS",
    "ImageDownloader",
    "NotificationImageCache",
    "NotificationMonitor",
    "NotificationQueue",
    "NotificationsFile",
    "QueuedNotification",
    "ReceiptSender",
    "SeenSession",
    "SessionState",
    "load_notifications",
    "save_notifications",
]

#: §5.2.2: пакет подтверждений «ознакомлен»/«отложено» на сервер
RECEIPTS_STATUS_PATH = "/api/v1/agent/receipts"
#: §7.4, Э9: откуда касса скачивает картинку тела уведомления (токен агента)
IMAGE_API_PATH = "/api/v1/agent/notifications/{notification_id}/images/{name}"
#: §7.4, Э9: сколько прочитанных уведомлений отдаём в «справочник» окна кассы
ARCHIVE_LIMIT = 30
#: §7.4, Э9: каталог локального кэша картинок рядом с `notifications.json`
IMAGE_CACHE_DIR_NAME = "notifications-images"
#: Расширение → тип картинки: тот же набор, что принимает сервер (§4.6)
IMAGE_MEDIA_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
DEFAULT_IMAGE_MEDIA_TYPE = "application/octet-stream"


def image_media_type(name: str) -> str:
    """Тип картинки по расширению — для отдачи из локального кэша (§7.4)."""
    extension = str(name).rsplit(".", 1)[-1].lower()
    return IMAGE_MEDIA_TYPES.get(extension, DEFAULT_IMAGE_MEDIA_TYPE)
#: §4.9: интервал возврата отложенного, если сервер его не прислал
DEFAULT_SNOOZE_MIN = 15
#: §4.6/§7.4: уровни, для которых служба дублирует показ через `msg.exe`
MSG_SEVERITIES: tuple[str, ...] = (
    str(NotificationSeverity.IMPORTANT),
    str(NotificationSeverity.CRITICAL),
)
#: Сколько уведомлений держим в очереди: касса — не архив, но и не теряет важное
MAX_QUEUE = 50
#: Через сколько часов сессия считается «ушедшей» и не попадает в состав (§4.8)
SESSION_STALE_HOURS = 12
#: Пауза досылки очереди подтверждений, если сервер не принял пакет (§7.4)
RETRY_DELAY_SEC = 30.0


class QueuedNotification(BaseModel):
    """Уведомление в файловой очереди кассы (`notifications.json`, §7.4).

    Очередь переживает перезапуск службы и недоступность сервера: уведомление
    уже принято и должно быть показано, даже если сервер «пропал».
    """

    model_config = ConfigDict(extra="ignore")

    id: int
    title: str
    body: str
    severity: NotificationSeverity = NotificationSeverity.INFO
    requires_ack: bool = False
    deadline_at: str | None = None
    repeat_every_min: int | None = None
    use_msg_exe: bool = True
    #: §4.9, П-2: интервал возврата отложенного — приходит от сервера
    snooze_min: int = DEFAULT_SNOOZE_MIN
    store_id: int | None = None
    queued_at: str | None = None
    #: Дублирующий канал `msg.exe` уже отработал: повторно не зовём (§7.4)
    msg_sent: bool = False
    #: Э9: `plain` — простой текст, `html` — разметка из редактора (§4.6)
    body_format: str = BODY_FORMAT_PLAIN
    #: Э9: имена картинок тела — их касса скачивает в локальный кэш (§7.4)
    images: list[str] = Field(default_factory=list)
    #: Э9: `true` — прочитанное исчезает из окна, `false` — остаётся справочником
    delete_after_read: bool = False

    @property
    def is_html(self) -> bool:
        """Тело пришло размеченным: окно кассы рисует его, а не показывает теги."""
        return str(self.body_format) == BODY_FORMAT_HTML

    def is_overdue(self, *, now: datetime | None = None) -> bool:
        """Дедлайн прошёл — показывать больше не нужно (§4.8 `expired`)."""
        deadline = parse_iso_z(self.deadline_at)
        return deadline is not None and deadline <= (now or utcnow())


class SessionState(BaseModel):
    """Состояние уведомления в конкретной сессии — основа `receipt` (§4.8, П-2).

    `reported=false` означает «подтверждение ещё не подтверждено сервером»:
    такие строки уезжают следующим пакетом `POST /api/v1/agent/receipts`.
    """

    model_config = ConfigDict(extra="ignore")

    notification_id: int
    session_id: str
    session_user: str
    status: ReceiptStatus = ReceiptStatus.PENDING
    shown_at: str | None = None
    acknowledged_at: str | None = None
    comment: str | None = None
    snooze_count: int = 0
    snooze_until: str | None = None
    reported: bool = False

    @property
    def is_acknowledged(self) -> bool:
        """«Ознакомлен» — только явное нажатие; отложенное таковым не считается (П-2)."""
        return (
            str(self.status) == str(ReceiptStatus.ACKNOWLEDGED)
            and self.acknowledged_at is not None
        )

    def snooze_deadline(self) -> datetime | None:
        """До какого времени уведомление скрыто кнопкой «Отложить» (П-2)."""
        return parse_iso_z(self.snooze_until)

    def to_item(self) -> ReceiptItem:
        """Превратить состояние в строку пакета подтверждений (§5.2.2)."""
        return ReceiptItem(
            notification_id=self.notification_id,
            session_id=self.session_id,
            session_user=self.session_user,
            status=self.status,
            comment=self.comment,
            snooze_until=self.snooze_until,
            shown_at=self.shown_at,
        )


class SeenSession(BaseModel):
    """Сессия Windows, которую видит TrayAgent (§7.4, Q50).

    Служба узнаёт о сессиях только от TrayAgent: он приходит на
    `GET /notifications/pending?session=...`. Персональных данных здесь нет —
    только логин Windows и идентификатор сессии (П-6).
    """

    model_config = ConfigDict(extra="ignore")

    session_id: str
    session_user: str
    last_seen_at: str | None = None

    def to_session(self) -> AgentSession:
        """Представление для `/health` и пакета подтверждений (§5.1.2)."""
        return AgentSession(user=self.session_user, sid=self.session_id)


class NotificationsFile(BaseModel):
    """Содержимое `C:\\MagazAgent\\notifications.json` (§7.4)."""

    model_config = ConfigDict(extra="ignore")

    notifications: list[QueuedNotification] = Field(default_factory=list)
    states: list[SessionState] = Field(default_factory=list)
    sessions: list[SeenSession] = Field(default_factory=list)




def load_notifications(path: str | Path) -> NotificationsFile | None:
    """Прочитать очередь; нет файла или он битый — `None` (служба не падает)."""
    target = Path(path)
    if not target.is_file():
        return None
    try:
        with target.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        logger.warning("Очередь уведомлений не прочитана: %s", target)
        return None
    if not isinstance(data, dict):
        return None
    try:
        return NotificationsFile.model_validate(data)
    except ValueError:
        logger.warning("Очередь уведомлений повреждена: %s", target)
        return None


def save_notifications(path: str | Path, data: NotificationsFile) -> Path:
    """Сохранить очередь атомарно (temp + `replace`, как профиль 1С и устройства).

    Если кассу выключат в момент записи, файл останется читаемым: либо старая
    очередь, либо новая (§7.4).
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data.model_dump(mode="json"), handle, ensure_ascii=False, indent=2)
    tmp.replace(target)
    logger.debug("Очередь уведомлений сохранена: %s", target)
    return target


class NotificationQueue:
    """Очередь уведомлений и состояния сессий — источник истины на кассе (§7.4)."""

    def __init__(
        self, *, path: Path | None = None, data: NotificationsFile | None = None
    ) -> None:
        self.path = Path(path) if path else None
        self._data = data or NotificationsFile()

    @classmethod
    def load(cls, path: str | Path | None) -> "NotificationQueue":
        """Поднять очередь с диска; файла нет — пустая очередь (§7.4)."""
        target = Path(path) if path else None
        stored = load_notifications(target) if target else None
        return cls(path=target, data=stored or NotificationsFile())

    # --- Содержимое ----------------------------------------------------------

    @property
    def notifications(self) -> list[QueuedNotification]:
        """Уведомления в очереди (в порядке поступления)."""
        return self._data.notifications

    @property
    def states(self) -> list[SessionState]:
        """Состояния по сессиям — то, из чего собираются `receipts` (§4.8)."""
        return self._data.states

    def find(self, notification_id: int) -> QueuedNotification | None:
        """Уведомление очереди по id."""
        for item in self._data.notifications:
            if int(item.id) == int(notification_id):
                return item
        return None

    def state(self, notification_id: int, session_id: str) -> SessionState | None:
        """Состояние уведомления в сессии (или `None`)."""
        for item in self._data.states:
            if (
                int(item.notification_id) == int(notification_id)
                and item.session_id == session_id
            ):
                return item
        return None

    def save(self) -> None:
        """Записать очередь на диск (если путь задан)."""
        if self.path is not None:
            save_notifications(self.path, self._data)

    def _trim(self) -> None:
        """Держим не больше `MAX_QUEUE` уведомлений; ушедшие уносят и состояния."""
        extra = len(self._data.notifications) - MAX_QUEUE
        if extra <= 0:
            return
        removed = {int(item.id) for item in self._data.notifications[:extra]}
        self._data.notifications = self._data.notifications[extra:]
        self._data.states = [
            item for item in self._data.states if int(item.notification_id) not in removed
        ]

    # --- Приём от сервера ----------------------------------------------------

    def accept(self, request: NotifyRequest) -> dict[str, object]:
        """Принять уведомление от сервера (`POST /notify`, §5.1.2, §7.4).

        Повторная доставка (retry диспетчера) не должна «сбрасывать» уже
        нажатое «Ознакомлен»: содержимое обновляется, состояния сессий — нет.
        Недоступность сервера на показ не влияет: уведомление уже в очереди.
        """
        queued = QueuedNotification(
            id=int(request.id),
            title=request.title,
            body=request.body,
            severity=request.severity,
            requires_ack=bool(request.requires_ack),
            deadline_at=request.deadline_at,
            repeat_every_min=request.repeat_every_min,
            use_msg_exe=bool(request.use_msg_exe),
            snooze_min=max(1, int(request.snooze_min or DEFAULT_SNOOZE_MIN)),
            store_id=request.store_id,
            queued_at=to_iso_z(utcnow()),
            # Э9: разметка, картинки и признак «удалять после прочтения» едут от
            # сервера до окна кассы без изменений (§4.6, §7.4)
            body_format=request.body_format,
            images=[name for name in request.images if is_safe_image_name(name)],
            delete_after_read=bool(request.delete_after_read),
        )
        existing = self.find(queued.id)
        if existing is not None:
            queued.msg_sent = existing.msg_sent
            index = self._data.notifications.index(existing)
            self._data.notifications[index] = queued
        else:
            self._data.notifications.append(queued)
        self._trim()
        self.save()

        sessions = len(self.sessions())
        logger.info(
            "Уведомление id=%s принято (уровень %s, сессий на кассе %s)",
            queued.id,
            queued.severity,
            sessions,
        )
        return {"queued": True, "sessions": sessions}

    # --- Сессии --------------------------------------------------------------

    def touch_session(
        self, session_id: str, session_user: str, *, now: datetime | None = None
    ) -> SeenSession:
        """Запомнить сессию, которая спросила уведомления (§7.4, Q50).

        Сессии узнаются только от TrayAgent: он приходит на
        `GET /notifications/pending?session=...`, поэтому «X из Y» считается по
        тем, кто реально работает за ПК (П-6).
        """
        moment = now or utcnow()
        for item in self._data.sessions:
            if item.session_id == session_id:
                if session_user:
                    item.session_user = session_user
                item.last_seen_at = to_iso_z(moment)
                return item
        created = SeenSession(
            session_id=session_id,
            session_user=session_user,
            last_seen_at=to_iso_z(moment),
        )
        self._data.sessions.append(created)
        return created

    def sessions(self, *, now: datetime | None = None) -> list[AgentSession]:
        """Активные сессии: ушедшие (`SESSION_STALE_HOURS`) в отчёт не попадают."""
        moment = now or utcnow()
        return [
            item.to_session()
            for item in self._data.sessions
            if not self._is_stale(item, now=moment)
        ]

    @staticmethod
    def _is_stale(session: SeenSession, *, now: datetime) -> bool:
        """Давно не приходившая сессия: пользователь вышел или перезагрузил ПК."""
        seen = parse_iso_z(session.last_seen_at)
        if seen is None:
            return False
        return (now - seen) > timedelta(hours=SESSION_STALE_HOURS)

    def prune_sessions(self, *, now: datetime | None = None) -> int:
        """Убрать ушедшие сессии из файла очереди (§7.4)."""
        moment = now or utcnow()
        keep = [
            item for item in self._data.sessions if not self._is_stale(item, now=moment)
        ]
        removed = len(self._data.sessions) - len(keep)
        if removed:
            self._data.sessions = keep
            self.save()
        return removed

    # --- Видимость для TrayAgent --------------------------------------------

    def pending_for(
        self, ref: SessionRef, *, now: datetime | None = None
    ) -> list[NotificationPendingItem]:
        """Что показать в сессии (`GET /notifications/pending`, §5.1.2, §7.4).

        Видимость решается здесь, а не в трее:

        * `acknowledged` — закрыто, больше не показывается;
        * `snoozed` — скрыто до `snooze_until` (П-2), после чего возвращается;
        * дедлайн прошёл — состояние закрывается `expired` и показ прекращается;
        * `info` не всплывает сам: тост показывается один раз, дальше уведомление
          ждёт в трее явного «Ознакомлен» (§7.4).
        """
        moment = now or utcnow()
        self.touch_session(ref.session_id, ref.session_user, now=moment)
        visible: list[NotificationPendingItem] = []
        changed = False

        for item in self._data.notifications:
            state = self.state(item.id, ref.session_id)
            if item.is_overdue(now=moment):
                if state is None:
                    # Сессия спросила уведомление уже после дедлайна: ей всё равно
                    # нужна строка отчёта — «просрочено», а не «ничего не было»
                    state = self._state_for(item.id, ref, now=moment)
                if self._expire(state):
                    changed = True
                continue
            if state is not None:
                if state.is_acknowledged:
                    continue
                if str(state.status) == str(ReceiptStatus.EXPIRED):
                    continue
                if str(state.status) == str(ReceiptStatus.SNOOZED):
                    hide_until = state.snooze_deadline()
                    if hide_until is not None and hide_until > moment:
                        continue

            visible.append(
                NotificationPendingItem(
                    id=item.id,
                    title=item.title,
                    body=item.body,
                    severity=item.severity,
                    requires_ack=bool(item.requires_ack),
                    repeat_every_min=item.repeat_every_min,
                    snooze_until=state.snooze_until if state is not None else None,
                    # Э9: окно кассы рисует разметку и картинки из своего кэша (§7.4)
                    body_format=item.body_format,
                    images=list(item.images),
                    delete_after_read=bool(item.delete_after_read),
                )
            )

        if changed:
            self.save()
        return visible

    def archive_for(self, ref: SessionRef) -> list[NotificationPendingItem]:
        """Что уже прочитано в этой сессии, но остаётся справочником (`GET /notifications/archive`, §7.4, Э9).

        Уведомление с `delete_after_read=false` не исчезает с кассы: кассир может
        вернуться к инструкции («как закрыть смену») в любой момент. Свежие — первыми,
        и не больше `ARCHIVE_LIMIT`: окно кассы — не архив, а подсказка под рукой.
        """
        items: list[NotificationPendingItem] = []
        for item in sorted(self._data.notifications, key=lambda entry: int(entry.id), reverse=True):
            if bool(item.delete_after_read):
                continue
            state = self.state(item.id, ref.session_id)
            if state is None or not state.is_acknowledged:
                continue
            items.append(
                NotificationPendingItem(
                    id=item.id,
                    title=item.title,
                    body=item.body,
                    severity=item.severity,
                    requires_ack=bool(item.requires_ack),
                    repeat_every_min=item.repeat_every_min,
                    body_format=item.body_format,
                    images=list(item.images),
                    delete_after_read=False,
                )
            )
            if len(items) >= ARCHIVE_LIMIT:
                break
        return items

    @staticmethod
    def _expire(state: SessionState | None) -> bool:
        """Закрыть состояние как `expired`; `True` — что-то изменилось (§4.8)."""
        if state is None or state.is_acknowledged:
            return False
        if str(state.status) == str(ReceiptStatus.EXPIRED):
            return False
        state.status = ReceiptStatus.EXPIRED
        state.reported = False
        return True

    # --- Кнопки окна (§5.1.2, П-2) ------------------------------------------

    def _state_for(
        self, notification_id: int, ref: SessionRef, *, now: datetime
    ) -> SessionState | None:
        """Состояние сессии; создаётся только для известного уведомления.

        Любое действие из сессии (показ, «Ознакомлен», «Отложить») заодно
        обновляет её в списке сессий: раз пользователь нажал кнопку, он работает
        за кассой, и «X из Y» должен его учитывать (§4.8, Q50).
        """
        if self.find(notification_id) is None:
            logger.warning(
                "Подтверждение по неизвестному уведомлению id=%s: сначала нужен /notify",
                notification_id,
            )
            return None
        self.touch_session(ref.session_id, ref.session_user, now=now)
        state = self.state(notification_id, ref.session_id)
        if state is None:
            state = SessionState(
                notification_id=int(notification_id),
                session_id=ref.session_id,
                session_user=ref.session_user,
            )
            self._data.states.append(state)
        elif ref.session_user:
            state.session_user = ref.session_user
        return state

    def mark_shown(
        self, notification_id: int, ref: SessionRef, *, now: datetime | None = None
    ) -> bool:
        """`POST /notifications/{id}/shown` — тост `info` показан (§7.4).

        Статус `shown` — это «увидел, но не подтвердил»: он нужен отчёту, чтобы
        админ отличал «не дошло» от «висит в трее».
        """
        moment = now or utcnow()
        state = self._state_for(notification_id, ref, now=moment)
        if state is None:
            return False
        if not state.is_acknowledged:
            state.status = ReceiptStatus.SHOWN
            if state.shown_at is None:
                state.shown_at = to_iso_z(moment)
        state.reported = False
        self.save()
        return True

    def acknowledge(
        self,
        notification_id: int,
        ref: SessionRef,
        *,
        comment: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """`POST /notifications/{id}/ack` — «Ознакомлен» (FR-26, §4.8)."""
        moment = now or utcnow()
        state = self._state_for(notification_id, ref, now=moment)
        if state is None:
            return False
        state.status = ReceiptStatus.ACKNOWLEDGED
        state.acknowledged_at = to_iso_z(moment)
        state.comment = (comment or "").strip() or None
        state.snooze_until = None
        if state.shown_at is None:
            state.shown_at = state.acknowledged_at
        state.reported = False
        self.save()
        logger.info(
            "Уведомление id=%s: «Ознакомлен» (сессия %s, пользователь %s)",
            notification_id,
            ref.session_id,
            ref.session_user,
        )
        return True

    def snooze(
        self, notification_id: int, ref: SessionRef, *, now: datetime | None = None
    ) -> str | None:
        """`POST /notifications/{id}/snooze` — «Отложить» (П-2, Д-10).

        Возвращает время возврата: TrayAgent показывает «отложено до». Статус
        остаётся **неознакомленным** — иначе отчёт врал бы в пользу админа.
        """
        moment = now or utcnow()
        item = self.find(notification_id)
        state = self._state_for(notification_id, ref, now=moment)
        if item is None or state is None:
            return None
        minutes = max(1, int(item.snooze_min or DEFAULT_SNOOZE_MIN))
        state.status = ReceiptStatus.SNOOZED
        state.snooze_count = int(state.snooze_count or 0) + 1
        state.snooze_until = to_iso_z(moment + timedelta(minutes=minutes))
        if state.shown_at is None:
            state.shown_at = to_iso_z(moment)
        state.reported = False
        self.save()
        logger.info(
            "Уведомление id=%s отложено на %s мин (сессия %s)",
            notification_id,
            minutes,
            ref.session_id,
        )
        return state.snooze_until

    def expire_overdue(self, *, now: datetime | None = None) -> int:
        """Проставить `expired` всему, что не ознакомлено к дедлайну (§4.8).

        Нужно там, где уведомление никто не запрашивал: сессия могла выйти из
        системы, и «дедлайн прошёл» должно быть видно в отчёте, а не ждать,
        пока пользователь снова войдёт.
        """
        moment = now or utcnow()
        expired = 0
        for item in self._data.notifications:
            if not item.is_overdue(now=moment):
                continue
            for state in self._data.states:
                if int(state.notification_id) == int(item.id) and self._expire(state):
                    expired += 1
        if expired:
            self.save()
        return expired

    # --- Досылка подтверждений (§7.4) ---------------------------------------

    def dirty_states(self) -> list[SessionState]:
        """Состояния, которые сервер ещё не подтвердил: их и досылаем."""
        return [item for item in self._data.states if not item.reported]

    def mark_reported(self, states: Sequence[SessionState]) -> None:
        """Отметить, что сервер принял эти подтверждения (§7.4)."""
        for item in states:
            item.reported = True
        self.save()

    def build_batch(self, store_id: int) -> ReceiptBatch:
        """Собрать пакет подтверждений вместе с составом сессий (§5.2.2, §4.8)."""
        return ReceiptBatch(
            store_id=int(store_id),
            receipts=[item.to_item() for item in self.dirty_states()],
            sessions=self.sessions(),
        )

    def has_pending_work(self) -> bool:
        """Есть ли что досылать — расписание спрашивает это перед отправкой."""
        return bool(self.dirty_states())

    # --- Дублирующий канал `msg.exe` (§7.4) ---------------------------------

    def msg_candidates(self, *, now: datetime | None = None) -> list[QueuedNotification]:
        """Что дублировать через `msg.exe`: `important`/`critical`, один раз (§7.4)."""
        moment = now or utcnow()
        return [
            item
            for item in self._data.notifications
            if str(item.severity) in MSG_SEVERITIES
            and item.use_msg_exe
            and not item.msg_sent
            and not item.is_overdue(now=moment)
        ]

    def mark_msg_sent(self, notification_id: int) -> None:
        """Запомнить, что дублирующий канал для уведомления уже отработал."""
        item = self.find(notification_id)
        if item is None:
            return
        item.msg_sent = True
        self.save()


def msg_text(item: QueuedNotification) -> str:
    """Текст для `msg.exe`: заголовок и тело одним сообщением (§7.4).

    `msg.exe` рисует обычное окно Windows, поэтому уровень важности попадает в
    первую строку: пользователь видит, насколько это срочно.
    """
    return f"[{str(item.severity).upper()}] {item.title}\n\n{item.body}"


class ReceiptSender:
    """Досылка подтверждений на сервер (§5.2.2, §7.4).

    Агент ходит на сервер сам (`POST /api/v1/agent/receipts`), поэтому
    httpx-клиент инжектируется: в тестах сеть подменяется `MockTransport` (§13).
    Пока сервер не принял пакет, состояния остаются неотправленными: данные не
    теряются, даже если касса перезагрузится (§7.4).
    """

    def __init__(
        self,
        config: AgentConfig,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.config = config
        self._client = client
        self._transport = transport
        self.timeout = float(timeout)

    @property
    def url(self) -> str:
        """Полный адрес точки приёма подтверждений на сервере."""
        base = (self.config.server_url or "").rstrip("/")
        return f"{base}{RECEIPTS_STATUS_PATH}"

    async def send(self, batch: ReceiptBatch) -> bool:
        """Отправить пакет; `False` — сервер не принял (пакет остаётся в очереди)."""
        if not self.config.server_url or not self.config.agent_token:
            logger.debug(
                "Адрес сервера или токен агента не заданы — подтверждения копим локально"
            )
            return False

        headers = {"X-Agent-Token": self.config.agent_token, "Accept": "application/json"}
        payload = batch.model_dump(mode="json")
        try:
            if self._client is not None:
                response = await self._client.post(self.url, json=payload, headers=headers)
            else:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self.timeout),
                    transport=self._transport,
                    trust_env=False,
                ) as client:
                    response = await client.post(self.url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            logger.debug(
                "Пакет подтверждений не доставлен (%s): %s",
                self.url,
                exc.__class__.__name__,
            )
            return False
        if response.status_code >= 400:
            logger.warning(
                "Сервер отклонил пакет подтверждений: %s %s",
                response.status_code,
                response.text[:200],
            )
            return False
        return True


class NotificationImageCache:
    """Кэш картинок тела уведомления на кассе (§7.4, Э9).

    Картинка скачивается с сервера один раз и дальше живёт файлом: окно уведомления
    показывает её и без сети, а перезапуск службы ничего не перекачивает. Файлы лежат
    рядом с очередью (`notifications-images/<id>/<имя>`), имя проверяется тем же
    правилом, что и на сервере (`shared.richtext`).
    """

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)

    def directory(self, notification_id: int) -> Path:
        """Каталог картинок одного уведомления."""
        return self.base_dir / str(int(notification_id))

    def path(self, notification_id: int, name: str) -> Path | None:
        """Путь к файлу картинки; `None` — имя небезопасно и в кэш не пускаем."""
        if not is_safe_image_name(name):
            return None
        return self.directory(notification_id) / name

    def has(self, notification_id: int, name: str) -> bool:
        """Картинка уже скачана — второй раз в сеть не идём."""
        path = self.path(notification_id, name)
        return bool(path and path.is_file())

    def store(self, notification_id: int, name: str, data: bytes) -> Path | None:
        """Положить картинку в кэш; `None` — имя небезопасно или данных нет."""
        path = self.path(notification_id, name)
        if path is None or not data:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def read(self, notification_id: int, name: str) -> bytes | None:
        """Байты картинки из кэша; `None` — файла нет (удалён вручную, не скачан)."""
        path = self.path(notification_id, name)
        if path is None or not path.is_file():
            return None
        return path.read_bytes()


class ImageDownloader:
    """Скачивание картинок уведомления с сервера в локальный кэш (§7.4, Э9).

    Агент ходит на сервер своим токеном (`X-Agent-Token`) — как и за подтверждениями.
    Недоступность сервера не сбой доставки: уведомление уже в очереди, а картинка
    догоняет кассу, когда пользователь открывает окно, — но только если есть сеть.
    """

    def __init__(
        self,
        config: AgentConfig,
        *,
        cache: NotificationImageCache,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.config = config
        self.cache = cache
        self._client = client
        self._transport = transport
        self.timeout = float(timeout)

    def url(self, notification_id: int, name: str) -> str:
        """Полный адрес картинки на сервере (токен агента, §7.4)."""
        base = (self.config.server_url or "").rstrip("/")
        path = IMAGE_API_PATH.format(notification_id=int(notification_id), name=name)
        return f"{base}{path}"

    async def fetch(self, notification_id: int, names: Sequence[str]) -> int:
        """Скачать недостающие картинки; возвращает число сохранённых файлов."""
        if not self.config.server_url or not self.config.agent_token:
            logger.debug("Адрес сервера или токен агента не заданы — картинки не качаем")
            return 0
        stored = 0
        for name in names:
            if self.cache.has(notification_id, name):
                continue
            data = await self._download(notification_id, name)
            if data is None:
                continue
            if self.cache.store(notification_id, name, data) is not None:
                stored += 1
        if stored:
            logger.info("Картинок уведомления id=%s скачано: %s", notification_id, stored)
        return stored

    async def _download(self, notification_id: int, name: str) -> bytes | None:
        """Одна картинка: сетевые сбои и отказы сервера — просто «не скачали»."""
        url = self.url(notification_id, name)
        headers = {"X-Agent-Token": self.config.agent_token or "", "Accept": "image/*"}
        try:
            if self._client is not None:
                response = await self._client.get(url, headers=headers)
            else:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self.timeout),
                    transport=self._transport,
                    trust_env=False,
                ) as client:
                    response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            logger.debug(
                "Картинка %s уведомления id=%s не скачана (%s)",
                name,
                notification_id,
                exc.__class__.__name__,
            )
            return None
        if response.status_code >= 400:
            logger.warning(
                "Сервер не отдал картинку %s (уведомление id=%s): %s",
                name,
                notification_id,
                response.status_code,
            )
            return None
        return response.content


class NotificationMonitor:
    """Расписание службы уведомлений (§7.4).

    Один цикл — четыре шага, и порядок здесь не случаен:

    1. убрать ушедшие сессии (`SESSION_STALE_HOURS`), чтобы «X из Y» не пухло от
       тех, кто уже вышел из системы;
    2. закрыть `expired` всё, у чего прошёл дедлайн;
    3. продублировать `important`/`critical` через `msg.exe` (один раз на
       уведомление) — дублирующий канал на случай, когда TrayAgent не запущен;
    4. дослать подтверждения на сервер, если он их ещё не видел.

    Уведомления приходят push-ом от сервера, поэтому цикл **не тянет** очередь из
    сети: он только приводит локальное состояние в порядок и досылает `ack`.
    """

    def __init__(
        self,
        queue: NotificationQueue,
        *,
        config: AgentConfig,
        sender: ReceiptSender,
        provider: ProcessProvider = DEFAULT_PROVIDER,
        interval_override_sec: float | None = None,
        startup_delay_sec: float = 5.0,
    ) -> None:
        self.queue = queue
        self.config = config
        self.sender = sender
        self.provider = provider
        self._interval_override = interval_override_sec
        self._startup_delay = max(0.0, float(startup_delay_sec))
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        #: Сколько раз пакет не доехал до сервера (диагностика и тесты)
        self.failed_sends = 0
        self.last_error: str | None = None

    # --- Расписание ----------------------------------------------------------

    @property
    def interval_sec(self) -> float:
        """Период цикла: `notifications_poll_sec` из конфига агента (§7.2)."""
        if self._interval_override is not None:
            return max(0.05, float(self._interval_override))
        return float(max(1, int(self.config.notifications_poll_sec or 20)))

    @property
    def is_running(self) -> bool:
        """Фоновая задача запущена и ещё не завершилась."""
        return self._task is not None and not self._task.done()

    def start(self) -> asyncio.Task[None]:
        """Запустить расписание (идемпотентно)."""
        if self.is_running:
            assert self._task is not None
            return self._task
        self._stop_event.clear()
        self._task = asyncio.get_running_loop().create_task(
            self.run(), name="notifications-monitor"
        )
        return self._task

    async def stop(self) -> None:
        """Остановить расписание и дождаться завершения задачи."""
        self._stop_event.set()
        task, self._task = self._task, None
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (TimeoutError, asyncio.TimeoutError):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def run(self) -> None:
        """Цикл «навести порядок → дослать → пауза» до сигнала остановки."""
        if self._startup_delay > 0:
            await self._sleep(self._startup_delay)
        while not self._stop_event.is_set():
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 - сбой цикла не должен ронять службу
                logger.exception("Сбой цикла уведомлений")
            await self._sleep(self.interval_sec)

    async def _sleep(self, seconds: float) -> None:
        """Пауза, прерываемая остановкой службы."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=max(0.0, seconds))
        except (TimeoutError, asyncio.TimeoutError):
            pass

    # --- Один цикл -----------------------------------------------------------

    async def poll_once(self) -> dict[str, int]:
        """Один проход расписания (§7.4)."""
        removed = self.queue.prune_sessions()
        expired = self.queue.expire_overdue()
        duplicated = await self.send_msg_duplicates()
        flushed = await self.flush()
        return {
            "sessions_removed": removed,
            "expired": expired,
            "msg_sent": duplicated,
            "flushed": flushed,
        }

    async def send_msg_duplicates(self) -> int:
        """Дублирующий канал `msg.exe */localhost` для `important`/`critical` (§7.4).

        Результат пишется в лог, а уведомление помечается обработанным: «нет
        сеансов» — нормальная ситуация для выключенного ПК, и звать `msg.exe`
        бесконечно нельзя. Показ в сессии всё равно делает TrayAgent.
        """
        if not self.config.msg_enabled:
            return 0
        sent = 0
        for item in self.queue.msg_candidates():
            await messenger.show_notification_message(msg_text(item), provider=self.provider)
            self.queue.mark_msg_sent(item.id)
            sent += 1
        return sent

    async def flush(self) -> int:
        """Дослать подтверждения; сервер не принял — они остаются в очереди (§7.4)."""
        if not self.queue.has_pending_work():
            return 0
        states = self.queue.dirty_states()
        batch = self.queue.build_batch(int(self.config.store_id or 0))
        if not await self.sender.send(batch):
            self.failed_sends += 1
            self.last_error = "сервер не принял пакет подтверждений"
            return 0
        self.queue.mark_reported(states)
        self.last_error = None
        logger.info(
            "Подтверждения доставлены на сервер: строк %s (сессий %s)",
            len(states),
            len(batch.sessions),
        )
        return len(states)


