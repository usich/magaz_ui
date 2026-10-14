"""Что и когда показывать в трее (§7.4) — без окон и без сети.

Служба отдаёт список незакрытых уведомлений, а решение «тост это или окно,
пора ли повторить» принимается здесь. Такую логику видно тестам: `pystray` и
`tkinter` в проверках не участвуют.

Правила §7.4 (повторяем их в коде один раз, чтобы не расходились):

* `info` — тост 3 с, фиксируется `shown` и **больше сам не всплывает**: ждёт в
  трее, пока пользователь не откроет окно и не нажмёт «Ознакомлен»;
* `important` — окно с кнопками «Ознакомлен» и «Отложить» (П-2), один раз;
* `critical` — то же окно, но поверх остальных окон и с повтором каждые
  `repeat_every_min` минут, пока не нажато «Ознакомлен»;
* отложенное до `snooze_until` служба в список не отдаёт, поэтому трей сам
  ничего «прятать» не должен — он доверяет ответу службы.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from shared.enums import NotificationSeverity
from shared.schemas import NotificationPendingItem
from shared.timeutil import utcnow

__all__ = ["TOAST_SECONDS", "TrayAction", "TrayPresenter"]

#: Сколько секунд висит тост `info` (§7.4)
TOAST_SECONDS = 3

#: Виды действий трея: тост и окно с кнопками
KIND_TOAST = "toast"
KIND_WINDOW = "window"


@dataclass(frozen=True)
class TrayAction:
    """Что трей должен сделать прямо сейчас: показать тост или окно."""

    kind: str
    notification: NotificationPendingItem

    @property
    def is_toast(self) -> bool:
        """Тост — самозакрывающееся окно `info`."""
        return self.kind == KIND_TOAST

    @property
    def is_window(self) -> bool:
        """Окно с кнопками «Ознакомлен» / «Отложить»."""
        return self.kind == KIND_WINDOW


class TrayPresenter:
    """Помнит, что уже показано в этой сессии, и решает, что показывать снова.

    Состояние — на одну сессию Windows и живёт столько же, сколько процесс
    трея: перезапуск трея (новый вход пользователя) означает, что окна нужно
    показать заново — иначе кассир не увидит важное уведомление после входа.
    """

    def __init__(self, *, toast_seconds: int = TOAST_SECONDS) -> None:
        self.toast_seconds = toast_seconds
        #: `id` уведомлений, по которым тост уже показан (повтор не нужен)
        self._toasted: set[int] = set()
        #: `id` открытых окон → время последнего показа (для повтора `critical`)
        self._windows: dict[int, datetime] = {}
        #: Последний ответ службы — он же список для меню «Непрочитанные»
        self._visible: list[NotificationPendingItem] = []

    # --- Что показывать -----------------------------------------------------

    def decide(
        self, items: list[NotificationPendingItem], *, now: datetime | None = None
    ) -> list[TrayAction]:
        """Действия на текущий опрос службы; пусто — показывать нечего."""
        moment = now or utcnow()
        actions: list[TrayAction] = []
        visible_ids = {item.id for item in items}

        for item in items:
            severity = str(item.severity)
            if severity == str(NotificationSeverity.INFO):
                if item.id in self._toasted:
                    continue
                self._toasted.add(item.id)
                actions.append(TrayAction(KIND_TOAST, item))
                continue

            if item.id not in self._windows:
                self._windows[item.id] = moment
                actions.append(TrayAction(KIND_WINDOW, item))
                continue

            if severity == str(NotificationSeverity.CRITICAL) and self._is_repeat_due(
                item, shown_at=self._windows[item.id], now=moment
            ):
                self._windows[item.id] = moment
                actions.append(TrayAction(KIND_WINDOW, item))

        self._forget_gone(visible_ids)
        self._visible = list(items)
        return actions

    def _is_repeat_due(
        self, item: NotificationPendingItem, *, shown_at: datetime, now: datetime
    ) -> bool:
        """Пора ли повторить `critical`: сервер задал `repeat_every_min` (§4.6).

        Без интервала повтор не выдумывается: окно остаётся открытым, пока его
        не закроют кнопкой «Ознакомлен».
        """
        repeat_min = item.repeat_every_min
        if not repeat_min or repeat_min <= 0:
            return False
        return (now - shown_at) >= timedelta(minutes=repeat_min)

    def _forget_gone(self, visible_ids: set[int]) -> None:
        """Убрать из памяти то, чего больше нет в ответе службы.

        Так выглядит `ack`/`snooze`/истечение дедлайна: служба перестала
        отдавать уведомление, и трей тоже забывает о нём — иначе тост не
        появится, если админ пришлёт уведомление с тем же `id` после отмены.
        """
        self._toasted &= visible_ids
        for gone in [item_id for item_id in self._windows if item_id not in visible_ids]:
            del self._windows[gone]

    # --- Список для меню ----------------------------------------------------

    @property
    def visible(self) -> list[NotificationPendingItem]:
        """Незакрытые уведомления сессии — их показывает меню трея (§7.4)."""
        return list(self._visible)

    @property
    def unread_count(self) -> int:
        """Сколько уведомлений ждёт «Ознакомлен» — подпись иконки в трее."""
        return len(self._visible)

    def forget(self, notification_id: int) -> None:
        """Забыть уведомление сразу после `ack`/`snooze`, не дожидаясь опроса."""
        self._toasted.discard(notification_id)
        self._windows.pop(notification_id, None)
        self._visible = [item for item in self._visible if item.id != notification_id]
