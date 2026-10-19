"""Общие элементы трея: плашка и карточка (спека 1.22).

Плашка — маленький цветной ярлык: категория («Продажи») или важность («критично»).
По требованию заказчика плашка показывается **в противоположной** строке к
текущей группировке: если список сгруппирован по важности, у уведомления видна
категория, а если по категориям — важность, и её цвет соответствует приоритету
(красный — критично и т. д.).

Элементы намеренно без логики данных: им передают готовые строки, поэтому их
легко проверить и переиспользовать в тосте, читалке и меню.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from client.tray.ui.theme import severity_color, severity_label, qcolor

__all__ = ["Badge", "category_badge", "severity_badge"]


class Badge(QLabel):
    """Цветная плашка: имя категории или уровень важности (спека 1.22, §7.4)."""

    def __init__(self, text: str, color: str) -> None:
        super().__init__(text)
        self.setObjectName("badge")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._apply(color)

    def _apply(self, color: str) -> None:
        """Окрасить плашку: фон — цвет, текст — белый (читается на любом тоне)."""
        tone = qcolor(color)
        # Контраст: на светлых тонах палитры текст делаем тёмным, на тёмных — белым
        light = tone.lightness() > 165
        foreground = "#0f172a" if light else "#ffffff"
        self.setStyleSheet(
            "QLabel#badge {"
            "  border-radius: 8px;"
            f" background: rgba({tone.red()}, {tone.green()}, {tone.blue()}, 220);"
            f" color: {foreground};"
            "  font-size: 11px;"
            "  font-weight: 600;"
            "  padding: 2px 8px;"
            "}"
        )


def category_badge(name: str, color: str) -> Badge:
    """Плашка категории — цвет приходит с сервера (1.22, §11.5)."""
    return Badge(str(name), str(color or "#64748b"))


def severity_badge(severity: object) -> Badge:
    """Плашка важности: цвет по приоритету, критично — красный (спека 1.22)."""
    badge = Badge(severity_label(severity), severity_color(severity))
    badge.setToolTip(f"уровень важности: {severity_label(severity)}")
    return badge
