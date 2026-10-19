"""Окно-читалка: полное тело уведомления (спека 1.22, §7.4).

Открывается кнопкой «Просмотреть» в тосте. По требованию заказчика окно можно
**перетаскивать по экрану** (за шапку) и **менять размер** (за правый нижний
угол): на кассе это удобно, когда сообщение длинное или его нужно отодвинуть от
рабочего места.

Кнопки только «Ознакомлен» и «Отложить» — системных «закрыть» и «свернуть» нет
(окно без рамки), как и у тоста. «Ознакомлен» закрывает читалку и открывает
меню; «Отложить» просто закрывает (1.21).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizeGrip,
    QVBoxLayout,
    QWidget,
)

from client.tray.ui.body import BodyView
from client.tray.ui.glass import GLASS_TINT_DENSE, PANEL_RADIUS, GlassWindow
from client.tray.ui.theme import (
    BASE_QSS,
    TEXT_MUTED,
    severity_color,
    severity_label,
)
from client.tray.ui.widgets import category_badge
from shared.schemas import NotificationPendingItem

__all__ = ["ReaderWindow", "reader_geometry"]

#: Стартовый размер читалки и минимальный: меньше — текст перестаёт читаться
READER_WIDTH = 560
READER_HEIGHT = 460
READER_MIN_WIDTH = 380
READER_MIN_HEIGHT = 260
#: Отступ от края: окно не липнет к рамке и не уезжает под панель задач
READER_MARGIN = 24
#: Высота полосы захвата (шапки), за которую окно перетаскивают
DRAG_BAR_HEIGHT = 44
#: Размер ручки изменения размера в правом нижнем углу
GRIP_SIZE = 18


def reader_geometry(screen_width: int, screen_height: int) -> tuple[int, int, int, int]:
    """Стартовая геометрия читалки — справа сверху, как и тост (§7.4).

    Функция чистая: тесты проверяют, что окно помещается на экране и не выходит
    за его границы даже на маленьком разрешении кассы.
    """
    width = min(READER_WIDTH, max(READER_MIN_WIDTH, int(screen_width) - 2 * READER_MARGIN))
    height = min(READER_HEIGHT, max(READER_MIN_HEIGHT, int(screen_height) - 2 * READER_MARGIN))
    x = max(READER_MARGIN, int(screen_width) - width - READER_MARGIN)
    y = max(READER_MARGIN, int(screen_height) // 6)
    return (x, y, width, height)


class ReaderWindow(GlassWindow):
    """Читалка уведомления: полное тело, перетаскивание и изменение размера (§7.4)."""

    radius = PANEL_RADIUS
    tint = GLASS_TINT_DENSE

    def __init__(
        self,
        item: NotificationPendingItem,
        *,
        on_ack: Callable[[int], None],
        on_snooze: Callable[[int], None],
        image_provider: Callable[[int, str], bytes | None] | None = None,
    ) -> None:
        super().__init__()
        self.item = item
        self._on_ack = on_ack
        self._on_snooze = on_snooze
        self._drag_origin: QPoint | None = None
        self.setObjectName("reader")
        self.setStyleSheet(BASE_QSS)
        self.setWindowTitle(f"{item.title} — уведомление #{item.id}")
        self.setMinimumSize(READER_MIN_WIDTH, READER_MIN_HEIGHT)
        self._build(image_provider)

    def _build(self, image_provider: Callable[[int, str], bytes | None] | None) -> None:
        """Собрать окно: шапка-захват, тело с разметкой и кнопки внизу."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 14)
        layout.setSpacing(10)

        layout.addWidget(self._build_header())

        self.body = BodyView(self.item, image_provider=image_provider)
        layout.addWidget(self.body, 1)

        layout.addWidget(self._build_footer())

    def _build_header(self) -> QWidget:
        """Шапка: заголовок с плашками. За неё окно перетаскивают мышью."""
        header = QWidget(self)
        header.setFixedHeight(DRAG_BAR_HEIGHT)
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        title = QLabel(self.item.title)
        title.setWordWrap(False)
        title.setStyleSheet(
            "font-size: 16px; font-weight: 600; "
            f"color: {severity_color(self.item.severity)};"
        )
        title.setToolTip("Потяните за эту строку, чтобы передвинуть окно")
        row.addWidget(title, 1)
        if self.item.category is not None:
            row.addWidget(
                category_badge(str(self.item.category.name), str(self.item.category.color))
            )
        level = QLabel(severity_label(self.item.severity))
        level.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        row.addWidget(level)
        return header

    def _build_footer(self) -> QWidget:
        """Подвал: только «Ознакомлен» и «Отложить»; в углу — ручка размера."""
        footer = QWidget(self)
        row = QHBoxLayout(footer)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        grip = QSizeGrip(footer)
        grip.setFixedSize(QSize(GRIP_SIZE, GRIP_SIZE))
        grip.setToolTip("Потяните, чтобы изменить размер окна")
        row.addWidget(grip, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch(1)

        snooze = QPushButton("Отложить")
        snooze.clicked.connect(self._snooze)
        ack = QPushButton("Ознакомлен")
        ack.setProperty("accent", True)
        ack.clicked.connect(self._ack)
        row.addWidget(snooze)
        row.addWidget(ack)
        return footer

    # --- Кнопки -------------------------------------------------------------

    def _ack(self) -> None:
        """«Ознакомлен»: подтвердить и закрыть читалку (меню открывает трей)."""
        self.close()
        self._on_ack(self.item.id)

    def _snooze(self) -> None:
        """«Отложить»: закрыть читалку; уведомление вернётся через snooze_min."""
        self.close()
        self._on_snooze(self.item.id)

    # --- Перетаскивание (заказчик: «должна быть возможность перетаскивать») ---

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

    def move_to_default(self) -> None:
        """Встать по стартовой геометрии текущего экрана (спека 1.22)."""
        screen = self.screen()
        geometry = screen.availableGeometry() if screen is not None else None
        width = geometry.width() if geometry is not None else READER_WIDTH
        height = geometry.height() if geometry is not None else READER_HEIGHT
        x, y, w, h = reader_geometry(width, height)
        if geometry is not None:
            x += geometry.x()
            y += geometry.y()
        self.setGeometry(x, y, w, h)
