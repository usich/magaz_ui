"""Что и когда показывать в трее (§7.4) — без окон и без сети.

Служба отдаёт список незакрытых уведомлений, а решение «пора всплыть снова»
принимается здесь. Такую логику видно тестам: Qt (`PySide6`) в проверках
не участвует — окна подменяются двойником.

Правила §7.4 (спека 1.21, переработка читалки):

* уведомление **любого** уровня сначала показывается маленьким тостом в правом
  верхнем углу с кнопками «Просмотреть» и «Отложить» — сам он не исчезает;
* окно-читалка (полное тело) открывается только по кнопке «Просмотреть»: трей
  не открывает окна сам;
* `critical` с `repeat_every_min` всплывает тостом снова по интервалу, пока не
  нажато «Ознакомлен»/«Отложить» (кассир не проспит критичное);
* отложенное до `snooze_until` служба в список не отдаёт, поэтому трей сам
  ничего «прятать» не должен — он доверяет ответу службы.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from collections.abc import Sequence

from shared.enums import NotificationSeverity
from shared.schemas import NotificationPendingItem
from shared.timeutil import utcnow

__all__ = [
    "GROUP_BY_CATEGORY",
    "GROUP_BY_SEVERITY",
    "KIND_TOAST",
    "TAB_ALL",
    "TAB_ARCHIVE",
    "TAB_TITLES",
    "TOAST_SECONDS",
    "TrayAction",
    "TrayGroup",
    "TrayPresenter",
    "build_tabs",
    "group_items",
    "items_for_tab",
    "panel_geometry",
]

#: Сколько секунд тост считался «свежим». С 1.21 тост сам не исчезает — значение
#: осталось для совместимости API (тесты, `--console`).
TOAST_SECONDS = 3

#: Виды действий трея: всплывающий тост с кнопками (спека 1.21)
KIND_TOAST = "toast"
#: Прежний вид действия «окно с кнопками» — с 1.21 окна открывает только кнопка
#: «Просмотреть», оставлен для разбора старых записей и совместимости.
KIND_WINDOW = "window"

#: Вкладка «все» и вкладка «справочник» — кроме уровней важности (§7.4, Э9)
TAB_ALL = "all"
TAB_ARCHIVE = "archive"

#: Подписи вкладок окна кассы: уровни совпадают с уровнями уведомления (§4.6)
TAB_TITLES: dict[str, str] = {
    TAB_ALL: "все",
    str(NotificationSeverity.INFO): "информация",
    str(NotificationSeverity.IMPORTANT): "важно",
    str(NotificationSeverity.CRITICAL): "критично",
    TAB_ARCHIVE: "справочник",
}

#: Доля площади экрана, которую занимает окно уведомления (§7.4: не больше 15 %)
PANEL_AREA_SHARE = 0.15
#: Какую часть ширины экрана занимает окно; высота считается из площади
PANEL_WIDTH_SHARE = 0.30
#: Минимальные размеры: мелкий экран не должен превращать окно в щель
PANEL_MIN_WIDTH = 360
PANEL_MIN_HEIGHT = 260

#: 1.22: как группируется список уведомлений в меню (§7.4)
GROUP_BY_SEVERITY = "severity"
GROUP_BY_CATEGORY = "category"
GROUP_MODES: tuple[str, ...] = (GROUP_BY_SEVERITY, GROUP_BY_CATEGORY)
#: Подпись категории для уведомлений без категории — их тоже надо куда-то отнести
NO_CATEGORY_TITLE = "без категории"
#: Цвет плашки уведомления без категории: нейтральный серый (§11.5)
NO_CATEGORY_COLOR = "#64748b"


@dataclass(frozen=True)
class TrayGroup:
    """Группа списка уведомлений: заголовок, цвет-плашка и элементы (§7.4, 1.22).

    Плашка нужна и в самом заголовке группы, и в противоположной строке: если
    список сгруппирован по важности, у каждой строки видно **категорию**, а если
    по категориям — **важность** (её цвет — из `SEVERITY_COLORS`). Так кассир
    всегда понимает и то, и другое, вне зависимости от выбранного режима (§7.4).
    """

    key: str
    title: str
    color: str
    items: list[NotificationPendingItem]

    @property
    def count(self) -> int:
        """Сколько уведомлений в группе — счётчик заголовка."""
        return len(self.items)


#: Цвет плашки важности: красный — критично, янтарный — важно, серый — информация
SEVERITY_COLORS: dict[str, str] = {
    str(NotificationSeverity.CRITICAL): "#dc2626",
    str(NotificationSeverity.IMPORTANT): "#f59e0b",
    str(NotificationSeverity.INFO): "#64748b",
}


def _category_key(item: NotificationPendingItem) -> str:
    """Ключ группы по категории: id категории или «без категории»."""
    return str(item.category.id) if item.category is not None else ""


def group_items(
    items: "Sequence[NotificationPendingItem]",
    *,
    by: str = GROUP_BY_SEVERITY,
) -> list[TrayGroup]:
    """Сгруппировать уведомления по важности или по категории (§7.4, 1.22).

    Группировка считается здесь, а не в окне: её проверяют тесты без GUI, а окно
    получает готовые группы с заголовком, цветом и элементами. Уведомление без
    категории попадает в отдельную группу «без категории», чтобы оно не терялось
    при группировке по категориям (§7.4).
    """
    order: list[TrayGroup] = []
    index: dict[str, TrayGroup] = {}

    if str(by) == GROUP_BY_CATEGORY:
        for item in items:
            key = _category_key(item)
            group = index.get(key)
            if group is None:
                if item.category is not None:
                    title = str(item.category.name)
                    color = str(item.category.color or NO_CATEGORY_COLOR)
                else:
                    title = NO_CATEGORY_TITLE
                    color = NO_CATEGORY_COLOR
                group = TrayGroup(key=key or "none", title=title, color=color, items=[])
                index[key] = group
                order.append(group)
            group.items.append(item)
        return order

    # По важности — фиксированный порядок: критично → важно → информация
    for severity in (
        NotificationSeverity.CRITICAL,
        NotificationSeverity.IMPORTANT,
        NotificationSeverity.INFO,
    ):
        key = str(severity)
        members = [item for item in items if str(item.severity) == key]
        if not members:
            continue
        order.append(
            TrayGroup(
                key=key,
                title=TAB_TITLES[key],
                color=SEVERITY_COLORS.get(key, NO_CATEGORY_COLOR),
                items=members,
            )
        )
    return order



@dataclass(frozen=True)
class TrayTab:
    """Вкладка окна уведомлений: ключ, подпись и счётчик (§7.4, Э9)."""

    key: str
    title: str
    count: int

    @property
    def is_archive(self) -> bool:
        """«Справочник» — прочитанное, что осталось на кассе."""
        return self.key == TAB_ARCHIVE


def build_tabs(
    unread: "Sequence[NotificationPendingItem]",
    archive: "Sequence[NotificationPendingItem]",
) -> list[TrayTab]:
    """Вкладки окна: «все», три уровня и «справочник» со счётчиками (§7.4, Э9).

    Счётчики считаются здесь, а не в окне: так их видно тестам, и трей не хранит
    второй список — он показывает то же, что отдала служба.
    """
    tabs = [TrayTab(TAB_ALL, TAB_TITLES[TAB_ALL], len(unread))]
    for severity in (
        NotificationSeverity.INFO,
        NotificationSeverity.IMPORTANT,
        NotificationSeverity.CRITICAL,
    ):
        key = str(severity)
        count = sum(1 for item in unread if str(item.severity) == key)
        tabs.append(TrayTab(key, TAB_TITLES[key], count))
    tabs.append(TrayTab(TAB_ARCHIVE, TAB_TITLES[TAB_ARCHIVE], len(archive)))
    return tabs


def items_for_tab(
    tab: str,
    unread: "Sequence[NotificationPendingItem]",
    archive: "Sequence[NotificationPendingItem]",
) -> list[NotificationPendingItem]:
    """Что показывает выбранная вкладка: «все», уровень важности или справочник."""
    if tab == TAB_ARCHIVE:
        return list(archive)
    if tab == TAB_ALL:
        return list(unread)
    return [item for item in unread if str(item.severity) == tab]


def panel_geometry(
    screen_width: int,
    screen_height: int,
    *,
    share: float = PANEL_AREA_SHARE,
    width_share: float = PANEL_WIDTH_SHARE,
    margin: int = 12,
) -> tuple[int, int, int, int]:
    """Геометрия окна в правом верхнем углу: `(x, y, ширина, высота)` (§7.4, Э9).

    Окно занимает не больше `share` площади экрана (0.15) и стоит в правом верхнем
    углу с отступом: кассир видит уведомление, но не теряет рабочее место. Высота
    считается из площади и ограничена снизу, чтобы текст оставался читаемым.
    """
    width = max(PANEL_MIN_WIDTH, int(screen_width * width_share))
    height = max(PANEL_MIN_HEIGHT, int(screen_width * screen_height * share / width))
    width = min(width, max(PANEL_MIN_WIDTH, screen_width - 2 * margin))
    height = min(height, max(PANEL_MIN_HEIGHT, screen_height - 2 * margin))
    return (max(margin, screen_width - width - margin), margin, width, height)


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
        #: `id` уведомлений → время последнего всплытия (для повтора `critical`)
        self._shown_at: dict[int, datetime] = {}
        #: Последний ответ службы — он же список для меню «Непрочитанные»
        self._visible: list[NotificationPendingItem] = []
        #: «Справочник»: прочитанное, что касса оставляет у себя (`delete_after_read=false`)
        self._archive: list[NotificationPendingItem] = []

    # --- Что показывать -----------------------------------------------------

    def decide(
        self, items: list[NotificationPendingItem], *, now: datetime | None = None
    ) -> list[TrayAction]:
        """Действия на текущий опрос службы; пусто — показывать нечего.

        С 1.21 уведомление любого уровня показывается **тостом** (кнопки
        «Просмотреть» и «Отложить»), поэтому здесь нет выбора «тост или окно»:
        окно-читалку открывает пользователь, а не презентер. Тост самостоятельного
        исчезает только кнопкой, поэтому повтор — это новое всплытие того же
        уведомления, и он оставлен только для `critical` с `repeat_every_min`.
        """
        moment = now or utcnow()
        actions: list[TrayAction] = []
        visible_ids = {item.id for item in items}

        for item in items:
            if item.id not in self._toasted:
                self._toasted.add(item.id)
                self._shown_at[item.id] = moment
                actions.append(TrayAction(KIND_TOAST, item))
                continue

            if self._is_repeat_due(
                item, shown_at=self._shown_at.get(item.id, moment), now=moment
            ):
                self._shown_at[item.id] = moment
                actions.append(TrayAction(KIND_TOAST, item))

        self._forget_gone(visible_ids)
        self._visible = list(items)
        return actions

    def _is_repeat_due(
        self, item: NotificationPendingItem, *, shown_at: datetime, now: datetime
    ) -> bool:
        """Пора ли всплыть тостом снова: сервер задал `repeat_every_min` (§4.6).

        Повтор есть только у `critical` — «важное» и «информация» показываются
        один раз и дальше ждут в трее. Без интервала повтор не выдумывается:
        уведомление ждёт кнопку «Ознакомлен».
        """
        if str(item.severity) != str(NotificationSeverity.CRITICAL):
            return False
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
        for gone in [item_id for item_id in self._shown_at if item_id not in visible_ids]:
            del self._shown_at[gone]

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
        self._shown_at.pop(notification_id, None)
        self._visible = [item for item in self._visible if item.id != notification_id]

    # --- Окно уведомлений (Э9) ----------------------------------------------

    def set_archive(self, items: "Sequence[NotificationPendingItem]") -> None:
        """Запомнить «справочник»: прочитанное, что осталось на кассе (§7.4, Э9)."""
        self._archive = list(items)

    @property
    def archive(self) -> list[NotificationPendingItem]:
        """Прочитанные уведомления, которые касса оставляет у себя."""
        return list(self._archive)

    def tabs(self) -> list[TrayTab]:
        """Вкладки окна со счётчиками — их рисует `TrayWindows` (§7.4, Э9)."""
        return build_tabs(self._visible, self._archive)
