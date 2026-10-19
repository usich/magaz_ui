"""Тост — всплывающее уведомление в правом верхнем углу (спека 1.22, §7.4).

Выглядит «как в Telegram»: маленькая тёмная стеклянная карточка в правом верхнем
углу с уровнем, заголовком, обрывком текста и двумя кнопками — «Просмотреть» и
«Отложить». Сам тост **не исчезает**: закрывается только одной из кнопок — это
требование заказчика (1.21), чтобы кассир ничего не пропустил.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from client.tray.richtext import body_blocks, outline
from client.tray.ui.glass import GLASS_TINT, TOAST_RADIUS, GlassWindow
from client.tray.ui.theme import (
    BASE_QSS,
    TEXT_MUTED,
    TOAST_HEIGHT,
    TOAST_WIDTH,
    severity_color,
)
from client.tray.ui.widgets import category_badge, severity_badge
from shared.schemas import NotificationPendingItem

__all__ = ["ToastWindow", "toast_geometry"]

#: Отступ от края экрана: тост не липнет к рамке (§7.4)
TOAST_MARGIN = 16
#: Сколько символов тела показывать в тосте — «обрывок», а не всё сообщение
TOAST_PREVIEW_LIMIT = 120


def toast_geometry(screen_width: int, screen_height: int) -> tuple[int, int, int, int]:
    """Геометрия тоста в правом верхнем углу: `(x, y, ширина, высота)` (§7.4).

    Позиция считается здесь, а не берётся из `panel_geometry`: тост — не меню,
    у него свои размеры. Функция чистая, поэтому её видно тестам.
    """
    width = TOAST_WIDTH
    height = TOAST_HEIGHT
    x = max(TOAST_MARGIN, int(screen_width) - width - TOAST_MARGIN)
    y = TOAST_MARGIN
    return (x, y, width, height)


class ToastWindow(GlassWindow):
    """Стеклянный тост с кнопками «Просмотреть» и «Отложить» (спека 1.22, §7.4)."""

    radius = TOAST_RADIUS
    tint = GLASS_TINT

    def __init__(
        self,
        item: NotificationPendingItem,
        *,
        on_view: Callable[[NotificationPendingItem], None],
        on_snooze: Callable[[int], None],
    ) -> None:
        super().__init__()
        self.item = item
        self._on_view = on_view
        self._on_snooze = on_snooze
        self.setObjectName("toast")
        self.setStyleSheet(BASE_QSS)
        self.setFixedSize(TOAST_WIDTH, TOAST_HEIGHT)
        self.setWindowTitle(f"Уведомление #{item.id}")
        self._build()

    def _build(self) -> None:
        """Собрать содержимое: шапка с плашками, заголовок, текст, кнопки."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(6)
        # Плашка важности — цвет по приоритету; критично выделяется красным
        header.addWidget(severity_badge(self.item.severity))
        if self.item.category is not None:
            header.addWidget(
                category_badge(str(self.item.category.name), str(self.item.category.color))
            )
        header.addStretch(1)
        hint = QLabel("✕")
        hint.setToolTip("Тост закрывается только кнопкой — так задумано")
        hint.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 13px;")
        header.addWidget(hint)
        layout.addLayout(header)

        title = QLabel(self.item.title)
        title.setWordWrap(True)
        title.setStyleSheet(
            f"font-weight: 600; font-size: 14px; color: {severity_color(self.item.severity)};"
        )
        layout.addWidget(title)

        preview = QLabel(self._preview())
        preview.setWordWrap(True)
        preview.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        layout.addWidget(preview)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        snooze = QPushButton("Отложить")
        snooze.clicked.connect(self._snooze)
        view = QPushButton("Просмотреть")
        view.setProperty("accent", True)
        view.clicked.connect(self._view)
        buttons.addWidget(snooze)
        buttons.addWidget(view)
        layout.addLayout(buttons)

    def _preview(self) -> str:
        """Обрывок тела: читается в две строки, без разметки (§7.4, Э9)."""
        blocks = body_blocks(self.item.body, self.item.body_format)
        return outline(blocks, limit=TOAST_PREVIEW_LIMIT) or "Без текста"

    # --- Кнопки -------------------------------------------------------------

    def _view(self) -> None:
        """«Просмотреть»: закрыть тост и открыть читалку (§7.4, 1.21)."""
        self.close()
        self._on_view(self.item)

    def _snooze(self) -> None:
        """«Отложить»: закрыть тост; меню не открывается (§7.4, 1.21)."""
        self.close()
        self._on_snooze(self.item.id)

    def move_to_corner(self) -> None:
        """Встать в правый верхний угол экрана (спека 1.22)."""
        screen = self.screen()
        geometry = screen.availableGeometry() if screen is not None else None
        width = geometry.width() if geometry is not None else TOAST_WIDTH
        height = geometry.height() if geometry is not None else TOAST_HEIGHT
        x, y, _w, _h = toast_geometry(width, height)
        if geometry is not None:
            x += geometry.x()
            y += geometry.y()
        self.move(x, y)

    def keyPressEvent(self, event) -> None:  # noqa: ANN001 - Qt
        """Escape откладывает уведомление — привычная клавиша, не «закрыть зря»."""
        if event.key() == Qt.Key.Key_Escape:
            self._snooze()
            return
        super().keyPressEvent(event)
