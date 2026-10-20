"""Меню уведомлений — согласованный макет 1.22: список и открытое сообщение.

Окно переключается между двумя состояниями — список и открытое сообщение.
Переключение делается прямым показом/скрытием двух страниц (а не через
`QStackedWidget`): в стеклянном окне стек на Windows ломал доставку кликов к
кнопкам страницы 2, прямое переключение видимости этого глюка не имеет:

* **список** — слева категории и важность с подсветкой непрочитанных, справа
  фильтры-пилюли и строки; кнопок внизу нет;
* **открытое сообщение** — «← Назад», плашки, заголовок, тело и кнопки
  «Отложить» / «Ознакомлен».

«Назад», «Отложить» и «Ознакомлен» возвращают к списку; «✕» закрывает окно.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizeGrip,
    QVBoxLayout,
    QWidget,
)

from client.tray.presenter import (
    GROUP_BY_CATEGORY,
    GROUP_BY_SEVERITY,
    NO_CATEGORY_COLOR,
    NO_CATEGORY_TITLE,
    TAB_ALL,
    build_tabs,
    category_nav,
    group_items,
    items_for_tab,
)
from client.tray.ui.body import BodyView
from client.tray.ui.glass import GLASS_TINT_DENSE, PANEL_RADIUS, GlassWindow
from client.tray.ui.theme import (
    BASE_QSS,
    TEXT_MUTED,
    severity_label,
)
from client.tray.ui.widgets import category_badge, severity_badge
from shared.enums import NotificationSeverity
from shared.schemas import NotificationPendingItem

__all__ = ["PanelWindow", "panel_mode_label", "row_text"]

#: Ширина левой колонки (категории + важность)
SIDEBAR_WIDTH = 200
#: Минимальная ширина правой области со списком
LIST_MIN_WIDTH = 320
#: Высота полосы захвата (шапки), за которую окно перетаскивают (как в читалке)
DRAG_BAR_HEIGHT = 44


def panel_mode_label(mode: str) -> str:
    """Подпись режима группировки: как сейчас сгруппирован список (1.22)."""
    return "по важности" if mode == GROUP_BY_SEVERITY else "по категориям"


def row_text(item: NotificationPendingItem, *, by: str) -> str:
    """Текст строки: заголовок и «противоположный» признак (§7.4, 1.22)."""
    if by == GROUP_BY_SEVERITY:
        name = item.category.name if item.category is not None else NO_CATEGORY_TITLE
        return f"{item.title}  ·  {name}"
    return f"{item.title}  ·  {severity_label(item.severity)}"


def _category_key(item: NotificationPendingItem) -> str:
    """Ключ категории для фильтра: id или «без категории»."""
    return str(item.category.id) if item.category is not None else "none"


def _dot_icon(color: str) -> QIcon:
    """Маленький цветной кружок — точка категории/важности в левой колонке."""
    pixmap = QPixmap(12, 12)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(str(color or "#64748b")))
    painter.drawEllipse(1, 1, 10, 10)
    painter.end()
    return QIcon(pixmap)


def _set_button_count(button: QPushButton, count: int) -> None:
    """Дописать счётчик непрочитанных к имени кнопки (1.22, §7.4)."""
    base = button.property("_side_base")
    if base is None:
        base = button.text()
        button.setProperty("_side_base", base)
    button.setText(f"{base}  ({count})" if count else base)


class PanelWindow(GlassWindow):
    """Окно «Уведомления»: список и открытое сообщение (согласованный макет 1.22)."""

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
        #: Активный фильтр-пилюля: «все», уровень важности или «справочник»
        self._tab = TAB_ALL
        #: Режим группировки строк правой области (по умолчанию — категории)
        self._mode = GROUP_BY_CATEGORY
        #: Чекбокс «Только непрочитанные»
        self._only_unread = False
        #: Показывать ли категории слева (чекбокс «Группировать по категориям»)
        self._show_categories = False
        #: Выбранная слева категория ("none" — без категории), `None` — любая
        self._filter_category: str | None = None
        #: Id уведомления в состоянии «открытое сообщение»
        self._selected: int | None = None
        #: Начало перетаскивания окна за шапку; `None` — мышь не удержана
        self._drag_origin: QPoint | None = None
        #: Живые кнопки слева — для подсветки непрочитанных
        self._category_buttons: dict[str, QPushButton] = {}
        self.setObjectName("panel")
        self.setStyleSheet(BASE_QSS)
        self.setWindowTitle("Уведомления")
        self._build()

    # --- Каркас ------------------------------------------------------------

    def _build(self) -> None:
        """Собрать окно: общая шапка и переключение состояний (экран 1/2).

        Две страницы кладутся в один контейнер, а не в `QStackedWidget`: в окне
        с `WA_TranslucentBackground` стек на Windows портил доставку мышиных
        событий к виджетам страницы 2 (кнопки «Назад»/«Отложить»/«Ознакомлен» не
        получали клики, пока QSizeGrip — нативная ручка — работал). Прямое
        переключение видимости обходит этот глюк и не меняет поведение.
        """
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(10)

        layout.addWidget(self._build_header())

        self._screen_list = self._build_screen_list()
        self._screen_message = self._build_screen_message()
        self._screen_message.hide()
        layout.addWidget(self._screen_list, 1)
        layout.addWidget(self._screen_message, 1)

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

    def _build_screen_list(self) -> QWidget:
        """Состояние «список»: слева категории/важность, справа фильтры и строки."""
        screen = QWidget(self)
        row = QHBoxLayout(screen)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        row.addWidget(self._build_sidebar())

        right = QWidget(screen)
        right.setMinimumWidth(LIST_MIN_WIDTH)
        columns = QVBoxLayout(right)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(8)
        columns.addWidget(self._build_filters())
        self._list = QListWidget(right)
        self._list.setSpacing(2)
        self._list.currentItemChanged.connect(self._on_row_changed)
        columns.addWidget(self._list, 1)
        row.addWidget(right, 1)
        return screen

    def _build_sidebar(self) -> QWidget:
        """Левая колонка: чекбоксы и категории (важность — только пилюли справа)."""
        side = QWidget(self)
        side.setFixedWidth(SIDEBAR_WIDTH)
        box = QVBoxLayout(side)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)

        self._only_check = QCheckBox("Только непрочитанные", side)
        self._only_check.toggled.connect(self._toggle_only_unread)
        box.addWidget(self._only_check)
        box.addSpacing(6)

        # 1.23: «Группировать по категориям» раскрывает список категорий слева —
        # по умолчанию свёрнут, чтобы не занимать место, пока фильтр не нужен
        self._categories_check = QCheckBox("Группировать по категориям", side)
        self._categories_check.toggled.connect(self._toggle_categories)
        box.addWidget(self._categories_check)

        cat_caption = QLabel("Категории", side)
        cat_caption.setProperty("role", "caption")
        box.addWidget(cat_caption)
        self._categories_box = QVBoxLayout()
        self._categories_box.setSpacing(2)
        box.addLayout(self._categories_box)

        box.addStretch(1)
        hint = QLabel("Выберите строку, чтобы открыть сообщение", side)
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size: 11px; padding: 8px 4px;")
        box.addWidget(hint)
        return side

    def _build_filters(self) -> QWidget:
        """Фильтры-пилюли: «все», уровни важности и «справочник»."""
        filters = QWidget(self)
        row = QHBoxLayout(filters)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self._filters_row = row
        return filters

    def _build_screen_message(self) -> QWidget:
        """Состояние «открытое сообщение»: «Назад», плашки, тело и кнопки."""
        screen = QWidget(self)
        box = QVBoxLayout(screen)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        top = QHBoxLayout()
        self._back_btn = QPushButton("← Назад")
        self._back_btn.clicked.connect(self._go_list)
        top.addWidget(self._back_btn)
        self._msg_header = QLabel("Уведомления")
        self._msg_header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._msg_header.setStyleSheet("font-size: 14px; font-weight: 600;")
        top.addWidget(self._msg_header, 1)
        top.addSpacing(60)
        box.addLayout(top)

        self._msg_badges = QHBoxLayout()
        self._msg_badges.setSpacing(6)
        box.addLayout(self._msg_badges)

        self._msg_title = QLabel("")
        self._msg_title.setWordWrap(True)
        self._msg_title.setStyleSheet("font-size: 16px; font-weight: 600;")
        box.addWidget(self._msg_title)

        self._msg_body = BodyView(
            self._placeholder_item(), image_provider=self._image_provider
        )
        box.addWidget(self._msg_body, 1)

        footer = QWidget(screen)
        foot_row = QHBoxLayout(footer)
        foot_row.setContentsMargins(0, 0, 0, 0)
        foot_row.setSpacing(8)
        grip = QSizeGrip(footer)
        grip.setFixedSize(QSize(18, 18))
        grip.setToolTip("Потяните, чтобы изменить размер окна")
        foot_row.addWidget(grip, 0, Qt.AlignmentFlag.AlignBottom)
        foot_row.addStretch(1)
        self._msg_snooze = QPushButton("Отложить")
        self._msg_snooze.clicked.connect(self._snooze_selected)
        self._msg_ack = QPushButton("Ознакомлен")
        self._msg_ack.setProperty("accent", True)
        self._msg_ack.clicked.connect(self._ack_selected)
        foot_row.addWidget(self._msg_snooze)
        foot_row.addWidget(self._msg_ack)
        box.addWidget(footer)
        return screen


    # --- Данные -------------------------------------------------------------

    def set_unread(self, items: list[NotificationPendingItem]) -> None:
        """Незакрытые уведомления сессии: счётчик, списки, подсветка (§7.4)."""
        self._unread = list(items)
        if self.isVisible():
            self._render()

    def set_archive(self, items: list[NotificationPendingItem]) -> None:
        """«Справочник»: прочитанное, что касса оставляет у себя (§7.4, Э9)."""
        self._archive = list(items)
        if self.isVisible():
            self._render()

    def _placeholder_item(self) -> NotificationPendingItem:
        """Пустое уведомление для тела: до открытия показывать нечего (§7.4)."""
        return NotificationPendingItem(
            id=0,
            title="",
            body="Выберите уведомление в списке слева",
            severity=NotificationSeverity.INFO,
            requires_ack=False,
        )

    def _visible_items(self) -> list[NotificationPendingItem]:
        """Строки правой области: фильтр-пилюля, чекбокс и категория (1.23)."""
        items = items_for_tab(self._tab, self._unread, self._archive)
        if self._only_unread:
            items = [item for item in items if not item.acknowledged]
        if self._filter_category is not None:
            items = [item for item in items if _category_key(item) == self._filter_category]
        return items

    # --- Рендер ------------------------------------------------------------

    def _render(self) -> None:
        """Перерисовать окно: шапка, боковая колонка, фильтры и список."""
        self._title.setText(f"Уведомления — непрочитанных: {len(self._unread)}")
        self._render_sidebar()
        self._render_filters()
        self._render_list()

    def _render_sidebar(self) -> None:
        """Категории слева: счётчики и подсветка непрочитанных (1.23).

        Категории показываются, только когда включён чекбокс «Группировать по
        категориям». Список категорий строится из элементов активной пилюли
        важности (`items_for_tab`), а не из всех непрочитанных: так навигация по
        категориям слева и отбор по важности справа работают одновременно —
        при выбранной важности слева видны только категории, в которых под неё
        есть уведомления (§7.4, 1.23).
        """
        self._category_buttons.clear()
        while self._categories_box.count():
            item = self._categories_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not self._show_categories:
            return
        visible = items_for_tab(self._tab, self._unread, self._archive)
        for key, name, color in category_nav(visible):
            button = self._make_side_button(
                name, color, 0, key == self._filter_category
            )
            button.clicked.connect(lambda _c=False, k=key: self._select_category(k))
            self._category_buttons[key] = button
            self._categories_box.addWidget(button)
        for key, button in self._category_buttons.items():
            count = sum(
                1 for i in visible if _category_key(i) == key and not i.acknowledged
            )
            button.setProperty("unread", count > 0)
            button.setProperty("active", key == self._filter_category)
        self._refresh_side_counts()

    def _refresh_side_counts(self) -> None:
        """Пересчитать счётчики на кнопках слева — по активной важности (1.23)."""
        visible = items_for_tab(self._tab, self._unread, self._archive)
        for key, button in self._category_buttons.items():
            count = sum(
                1 for i in visible if _category_key(i) == key and not i.acknowledged
            )
            _set_button_count(button, count)

    def _make_side_button(self, name: str, color: str, count: int, active: bool) -> QPushButton:
        """Кнопка слева: цветная точка, имя, счётчик непрочитанных (1.22)."""
        button = QPushButton(name)
        button.setProperty("side", True)
        button.setProperty("active", active)
        _set_button_count(button, count)
        button.setIcon(_dot_icon(color))
        return button


    def _render_filters(self) -> None:
        """Пилюли: «все», уровни важности и «справочник» со счётчиками (Э9)."""
        while self._filters_row.count():
            child = self._filters_row.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.deleteLater()
        for tab in build_tabs(self._unread, self._archive):
            button = QPushButton(f"{tab.title} ({tab.count})")
            button.setProperty("pill", True)
            button.setProperty("active", tab.key == self._tab)
            if not tab.is_archive and tab.count:
                button.setProperty("unread", True)
            button.clicked.connect(lambda _c=False, key=tab.key: self._select_tab(key))
            self._filters_row.addWidget(button)
        self._filters_row.addStretch(1)

    def _render_list(self) -> None:
        """Заполнить список правой области с группировкой и плашками (§7.4)."""
        self._list.blockSignals(True)
        self._list.clear()
        self._list.blockSignals(False)
        groups = group_items(self._visible_items(), by=self._mode)
        if not groups:
            empty = QListWidgetItem("Нет уведомлений")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            empty.setForeground(QBrush(QColor(TEXT_MUTED)))
            self._list.addItem(empty)
            return
        for group in groups:
            heading = QListWidgetItem(f"{group.title}  ·  {group.count}")
            heading.setFlags(Qt.ItemFlag.NoItemFlags)
            heading.setForeground(QBrush(QColor(TEXT_MUTED)))
            heading.setSizeHint(QSize(0, 28))
            self._list.addItem(heading)
            for item in group.items:
                row_item = QListWidgetItem()
                row_item.setData(Qt.ItemDataRole.UserRole, item.id)
                row_item.setSizeHint(QSize(0, 40))
                self._list.addItem(row_item)
                self._list.setItemWidget(row_item, self._make_row_widget(item))

    def _make_row_widget(self, item: NotificationPendingItem) -> QWidget:
        """Строка списка: плашка важности, заголовок и состояние (§7.4)."""
        widget = QWidget(self)
        inner = QHBoxLayout(widget)
        inner.setContentsMargins(4, 2, 6, 2)
        inner.setSpacing(8)
        badge = severity_badge(item.severity)
        inner.addWidget(badge)
        title = QLabel(item.title)
        title.setStyleSheet("border: none; background: transparent; font-size: 13px;")
        inner.addWidget(title, 1)
        if item.snooze_until:
            note = QLabel("отложено")
            note.setProperty("muted", True)
            note.setStyleSheet("border: none; background: transparent; font-size: 11px;")
            inner.addWidget(note)
        if item.acknowledged:
            note = QLabel("прочитано")
            note.setProperty("muted", True)
            note.setStyleSheet("border: none; background: transparent; font-size: 11px;")
            inner.addWidget(note)
        return widget

    def _on_row_changed(self, current: QListWidgetItem | None, _prev=None) -> None:
        """Клик по строке открывает сообщение (состояние 2) — согласованный макет."""
        if current is None or current.flags() & Qt.ItemFlag.ItemIsEnabled == 0:
            return
        item_id = current.data(Qt.ItemDataRole.UserRole)
        if item_id is None:
            return
        self._open_item(int(item_id))


    # --- Экшены -------------------------------------------------------------

    def _select_tab(self, key: str) -> None:
        """Фильтр-пилюля: «все» или уровень важности (§7.4)."""
        self._tab = key
        self._render()

    def _select_category(self, key: str) -> None:
        """Клик по категории слева: фильтровать список (повтор — снять фильтр)."""
        self._filter_category = None if self._filter_category == key else key
        self._render()

    def _toggle_categories(self, checked: bool) -> None:
        """Чекбокс «Группировать по категориям»: раскрыть/свернуть категории (1.23).

        Выключение фильтра по категориям снимает активную категорию: категории
        исчезают слева, но список остаётся полным (важность — пилюлями справа).
        """
        self._show_categories = checked
        if not checked:
            self._filter_category = None
        self._render()

    def _toggle_only_unread(self, checked: bool) -> None:
        """Чекбокс «Только непрочитанные»: показывать лишь незакрытые (§7.4)."""
        self._only_unread = checked
        self._render()

    def _open_item(self, item_id: int) -> None:
        """Открыть сообщение: заполнить экран 2 и переключить видимость (макет 1.22)."""
        item = next(
            (
                i
                for i in items_for_tab(TAB_ALL, self._unread, self._archive)
                if i.id == item_id
            ),
            None,
        )
        if item is None:
            return
        self._selected = item.id
        self._render_message(item)
        self._screen_list.hide()
        self._screen_message.show()

    def _render_message(self, item: NotificationPendingItem) -> None:
        """Заполнить состояние «открытое сообщение»: плашки, заголовок, тело."""
        self._msg_header.setText(f"Уведомления · сообщение #{item.id}")
        self._msg_title.setText(item.title)
        while self._msg_badges.count():
            child = self._msg_badges.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.deleteLater()
        self._msg_badges.addWidget(severity_badge(item.severity))
        if item.category is not None:
            self._msg_badges.addWidget(
                category_badge(str(item.category.name), str(item.category.color))
            )
        self._msg_badges.addStretch(1)
        # Прочитанное осталось справочником в своей категории: действий нет,
        # окно открыто только для просмотра (§7.4, 1.24).
        read = item.acknowledged
        self._msg_snooze.setVisible(not read)
        self._msg_ack.setVisible(not read)
        self._msg_body.render_item(item)

    def _go_list(self) -> None:
        """«← Назад» / «Отложить» / «Ознакомлен»: вернуться к списку (состояние 1)."""
        self._screen_message.hide()
        self._screen_list.show()
        self._selected = None
        self._render()

    def _ack_selected(self) -> None:
        """«Ознакомлен»: подтвердить и вернуться к списку (сценарий заказчика)."""
        if self._selected is None:
            return
        item_id = self._selected
        self._on_ack(item_id)
        self._go_list()

    def _snooze_selected(self) -> None:
        """«Отложить»: отложить и вернуться к списку (§7.4, 1.21)."""
        if self._selected is None:
            return
        item_id = self._selected
        self._on_snooze(item_id)
        self._go_list()

    def _close(self) -> None:
        """Крестик меню: окно уходит в трей, уведомления остаются (§7.4)."""
        self.hide()
        self._on_close()

    # --- Перетаскивание (заказчик: окно должно двигаться за шапку) ----------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: ANN001 - Qt
        """Начать перетаскивание, если нажатие пришлось на шапку (спека 1.22)."""
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.position().y() <= DRAG_BAR_HEIGHT
        ):
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: ANN001 - Qt
        """Двигать окно за курсором, пока кнопка удержана (спека 1.22)."""
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: ANN001 - Qt
        """Отпустить мышь: перетаскивание закончилось (спека 1.22)."""
        self._drag_origin = None
        super().mouseReleaseEvent(event)

    @property
    def selected_id(self) -> int | None:
        """Id открытого уведомления — по нему работают кнопки экрана 2."""
        return self._selected

    @property
    def mode(self) -> str:
        """Режим группировки списка: по важности или по категориям (1.22)."""
        return self._mode

    # --- Геометрия ---------------------------------------------------------

    def move_to_default(self) -> None:
        """Встать в правый верхний угол: под двухколоночный макет (§7.4, 1.22)."""
        screen = self.screen()
        geometry = screen.availableGeometry() if screen is not None else None
        width = geometry.width() if geometry is not None else 1920
        height = geometry.height() if geometry is not None else 1080
        win_w = min(940, max(LIST_MIN_WIDTH + SIDEBAR_WIDTH, int(width * 0.62)))
        win_h = min(660, max(420, int(height * 0.72)))
        x = max(12, width - win_w - 12)
        y = 12
        if geometry is not None:
            x += geometry.x()
            y += geometry.y()
        self.setGeometry(x, y, win_w, win_h)

    def showEvent(self, event) -> None:  # noqa: ANN001 - Qt
        """При показе — встать в угол, применить стекло и отрисовать данные."""
        if not self.property("_placed"):
            self.setProperty("_placed", True)
            self.move_to_default()
        # Данные могли прийти до показа (`set_unread` при скрытом окне) — рисуем
        self._render()
        super().showEvent(event)

