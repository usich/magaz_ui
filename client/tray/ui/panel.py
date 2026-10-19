"""Меню уведомлений: список, вкладки и переключатель группировки (спека 1.22, §7.4).

Единственное окно с крестиком «✕»: тост и читалка закрываются только кнопками, а
меню можно закрыть и мышью — как обычное окно. По требованию заказчика список
группируется **по важности** или **по категориям**, и рядом с заголовком всегда
виден «противоположный» признак плашкой:

* группировка по важности → у строки плашка категории;
* группировка по категориям → у строки плашка важности (красный — критично).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from client.tray.presenter import (
    GROUP_BY_CATEGORY,
    GROUP_BY_SEVERITY,
    TAB_ALL,
    TAB_ARCHIVE,
    build_tabs,
    group_items,
    items_for_tab,
)
from client.tray.ui.body import BodyView
from client.tray.ui.glass import GLASS_TINT_DENSE, PANEL_RADIUS, GlassWindow
from client.tray.ui.theme import BASE_QSS, severity_label
from shared.enums import NotificationSeverity
from shared.schemas import NotificationPendingItem

__all__ = ["PanelWindow", "panel_mode_label", "row_text"]


def panel_mode_label(mode: str) -> str:
    """Подпись переключателя: как сейчас сгруппирован список (1.22)."""
    return "по важности" if mode == GROUP_BY_SEVERITY else "по категориям"


class PanelWindow(GlassWindow):
    """Меню уведомлений: вкладки, список с группировкой и тело выбранного (§7.4)."""

    radius = PANEL_RADIUS
    tint = GLASS_TINT_DENSE

    def __init__(
        self,
        *,
        on_ack: Callable[[int], None],
        on_snooze: Callable[[int], None],
        on_close: Callable[[], None],
        image_provider: Callable[[int, str], bytes | None] | None = None,
    ) -> None:
        super().__init__()
        self._on_ack = on_ack
        self._on_snooze = on_snooze
        self._on_close = on_close
        self._image_provider = image_provider
        self._unread: list[NotificationPendingItem] = []
        self._archive: list[NotificationPendingItem] = []
        self._tab = TAB_ALL
        self._mode = GROUP_BY_SEVERITY
        self._selected: int | None = None
        self.setObjectName("panel")
        self.setStyleSheet(BASE_QSS)
        self.setWindowTitle("Уведомления")
        self._build()

    def _build(self) -> None:
        """Собрать окно: шапка с крестиком, вкладки, переключатель, список, тело."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(8)

        layout.addWidget(self._build_header())
        self._tabs_row = QHBoxLayout()
        self._tabs_row.setSpacing(6)
        tabs_holder = QWidget(self)
        tabs_holder.setLayout(self._tabs_row)
        layout.addWidget(tabs_holder)

        self._mode_button = QPushButton()
        self._mode_button.setProperty("flat", True)
        self._mode_button.clicked.connect(self._toggle_mode)
        mode_row = QHBoxLayout()
        mode_row.addWidget(self._mode_button)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        panes = QHBoxLayout()
        panes.setSpacing(10)
        self._list = QListWidget(self)
        self._list.setFixedWidth(240)
        self._list.currentRowChanged.connect(self._on_row)
        panes.addWidget(self._list, 0)
        self._body = BodyView(self._placeholder_item(), image_provider=self._image_provider)
        panes.addWidget(self._body, 1)
        layout.addLayout(panes, 1)

        layout.addWidget(self._build_footer())

    def _placeholder_item(self) -> NotificationPendingItem:
        """Пустое уведомление для тела: до выбора показывать нечего (§7.4)."""
        return NotificationPendingItem(
            id=0,
            title="",
            body="Выберите уведомление слева",
            severity=NotificationSeverity.INFO,
            requires_ack=False,
        )

    def _build_header(self) -> QWidget:
        """Шапка: счётчик непрочитанных и крестик «✕» — окно закрывается мышью."""
        header = QWidget(self)
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        self._title = QLabel("Уведомления")
        self._title.setStyleSheet("font-size: 16px; font-weight: 600;")
        row.addWidget(self._title, 1)
        close = QPushButton("✕")
        close.setProperty("flat", True)
        close.setToolTip("Закрыть окно: уведомления останутся в значке")
        close.clicked.connect(self._close)
        row.addWidget(close)
        return header

    def _build_footer(self) -> QWidget:
        """Подвал: «Ознакомлен» и «Отложить» для выбранного уведомления (§7.4)."""
        footer = QWidget(self)
        row = QHBoxLayout(footer)
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        snooze = QPushButton("Отложить")
        snooze.clicked.connect(self._snooze_selected)
        ack = QPushButton("Ознакомлен")
        ack.setProperty("accent", True)
        ack.clicked.connect(self._ack_selected)
        row.addWidget(snooze)
        row.addWidget(ack)
        return footer

    def _close(self) -> None:
        """Крестик меню: окно уходит в трей, уведомления остаются (§7.4)."""
        self.hide()
        self._on_close()

    # --- Данные -------------------------------------------------------------

    def set_unread(self, items: list[NotificationPendingItem]) -> None:
        """Незакрытые уведомления сессии: вкладка «все» и счётчик (§7.4)."""
        self._unread = list(items)
        if self.isVisible():
            self._render()

    def set_archive(self, items: list[NotificationPendingItem]) -> None:
        """«Справочник»: прочитанное, что касса оставляет у себя (§7.4, Э9)."""
        self._archive = list(items)
        if self.isVisible():
            self._render()

    @property
    def selected_id(self) -> int | None:
        """Id выбранного уведомления — по нему работают кнопки подвала."""
        return self._selected

    @property
    def mode(self) -> str:
        """Текущий режим группировки: по важности или по категориям (1.22)."""
        return self._mode

    def _toggle_mode(self) -> None:
        """Переключить группировку «по важности ↔ по категориям» (спека 1.22)."""
        self._mode = (
            GROUP_BY_CATEGORY if self._mode == GROUP_BY_SEVERITY else GROUP_BY_SEVERITY
        )
        self._selected = None
        self._render()

    def _current_items(self) -> list[NotificationPendingItem]:
        """Уведомления активной вкладки: «все» или «справочник» (§7.4)."""
        return items_for_tab(self._tab, self._unread, self._archive)

    def _render(self) -> None:
        """Перерисовать окно: счётчик, вкладки, переключатель и список (§7.4)."""
        self._title.setText(f"Уведомления — непрочитанных: {len(self._unread)}")

        while self._tabs_row.count():
            child = self._tabs_row.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.deleteLater()
        for tab in build_tabs(self._unread, self._archive):
            label = (
                f"{tab.title} ({tab.count})"
                if not tab.is_archive
                else f"справочник ({tab.count})"
            )
            button = QPushButton(label)
            button.setProperty("flat", tab.key != self._tab)
            button.clicked.connect(lambda _c=False, key=tab.key: self._select_tab(key))
            self._tabs_row.addWidget(button)
        self._tabs_row.addStretch(1)

        self._mode_button.setText(f"группировка: {panel_mode_label(self._mode)}")
        self._render_list()

    def _render_list(self) -> None:
        """Заполнить список с группировкой и подписью признака (1.22, §7.4)."""
        items = self._current_items()
        self._list.clear()
        if not items:
            empty = QListWidgetItem(
                "Справочник пуст." if self._tab == TAB_ARCHIVE else "Нет уведомлений"
            )
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self._list.addItem(empty)
            self._selected = None
            self._body.render_item(self._placeholder_item())
            return

        ids: list[int] = []
        for group in group_items(items, by=self._mode):
            heading = QListWidgetItem(f"{group.title} · {group.count}")
            heading.setFlags(Qt.ItemFlag.NoItemFlags)
            heading.setForeground(QBrush(QColor(group.color)))
            self._list.addItem(heading)
            for item in group.items:
                row = QListWidgetItem(row_text(item, by=self._mode))
                row.setData(Qt.ItemDataRole.UserRole, item.id)
                self._list.addItem(row)
                ids.append(item.id)

        if self._selected not in ids:
            self._selected = ids[0] if ids else None
        if self._selected is not None:
            self._select_row(self._selected)

    def _select_row(self, notification_id: int) -> None:
        """Выделить строку по id: подсветка и тело выбранного уведомления (§7.4)."""
        for index in range(self._list.count()):
            entry = self._list.item(index)
            if entry.data(Qt.ItemDataRole.UserRole) == notification_id:
                self._list.setCurrentRow(index)
                return

    def _on_row(self, index: int) -> None:
        """Пользователь выбрал строку: показать тело уведомления (§7.4)."""
        if index < 0:
            return
        entry = self._list.item(index)
        value = entry.data(Qt.ItemDataRole.UserRole)
        if value is None:
            return
        self._selected = int(value)
        item = next((it for it in self._current_items() if it.id == self._selected), None)
        if item is not None:
            self._body.render_item(item)

    def _select_tab(self, key: str) -> None:
        """Переключить вкладку: «все» или «справочник» и перерисовать (§7.4)."""
        self._tab = key
        self._selected = None
        self._render()

    # --- Кнопки подвала -----------------------------------------------------

    def _ack_selected(self) -> None:
        """«Ознакомлен» в меню: подтвердить выбранное и перерисовать (§7.4)."""
        if self._selected is not None:
            self._on_ack(self._selected)
            self._render()

    def _snooze_selected(self) -> None:
        """«Отложить» в меню: уведомление вернётся через snooze_min (§7.4)."""
        if self._selected is not None:
            self._on_snooze(self._selected)
            self._render()


def row_text(item: NotificationPendingItem, *, by: str) -> str:
    """Текст строки списка: заголовок и подпись «противоположного» признака.

    Группировка по важности → в строке видно категорию; по категориям → важность.
    Так выполняется требование заказчика: при любой группировке рядом с заголовком
    есть признак второго измерения (§7.4, 1.22).
    """
    if by == GROUP_BY_SEVERITY:
        if item.category is not None:
            return f"{item.title}  ·  {item.category.name}"
        return f"{item.title}  ·  без категории"
    return f"{item.title}  ·  {severity_label(item.severity)}"

