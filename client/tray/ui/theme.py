"""Тёмная тема трея: палитра, размеры и QSS (спека 1.22).

Всё оформление собрано здесь, чтобы цвета важности и категорий не расползались
по окнам: тост, читалка и меню берут один и тот же словарь. Цвета категорий
приходят с сервера (`NotificationPendingItem.category.color`), а цвета важности
заданы на кассе — они совпадают с палитрой `presenter.SEVERITY_COLORS`, чтобы
плашки в окне и в списке выглядели одинаково.
"""

from __future__ import annotations

from PySide6.QtGui import QColor

from client.tray.presenter import SEVERITY_COLORS
from shared.enums import NotificationSeverity

__all__ = [
    "ACCENT",
    "BG_CARD",
    "BG_CARD_HOVER",
    "BG_INPUT",
    "BORDER",
    "FONT_FAMILY",
    "SEVERITY_COLOR",
    "SEVERITY_LABEL",
    "TEXT",
    "TEXT_MUTED",
    "TOAST_HEIGHT",
    "TOAST_WIDTH",
    "severity_color",
    "severity_label",
    "qcolor",
]

#: Семейство шрифта: Segoe UI есть на всех кассах Windows, Inter — если стоит
FONT_FAMILY = '"Segoe UI Variable", "Segoe UI", Inter, "Noto Sans", sans-serif'

#: Текст и фон карточек поверх стекла
TEXT = "#f8fafc"
TEXT_MUTED = "#94a3b8"
ACCENT = "#38bdf8"
BORDER = "rgba(148, 163, 184, 0.22)"

#: Фон вложенных карточек и полей: полупрозрачный, чтобы стекло читалось
BG_CARD = "rgba(30, 41, 59, 0.55)"
BG_CARD_HOVER = "rgba(51, 65, 85, 0.75)"
BG_INPUT = "rgba(15, 23, 42, 0.55)"

#: Размер тоста: маленький, но помещает заголовок, обрывок текста и две кнопки
TOAST_WIDTH = 380
TOAST_HEIGHT = 132

#: Подписи уровней — как в presenter.TAB_TITLES (единый словарь для кассы)
SEVERITY_LABEL: dict[str, str] = {
    str(NotificationSeverity.INFO): "информация",
    str(NotificationSeverity.IMPORTANT): "важно",
    str(NotificationSeverity.CRITICAL): "критично",
}
SEVERITY_COLOR: dict[str, str] = dict(SEVERITY_COLORS)


def severity_label(severity: object) -> str:
    """Человекочитаемая подпись уровня важности (§4.6)."""
    key = str(severity)
    return SEVERITY_LABEL.get(key, key)


def severity_color(severity: object) -> str:
    """Цвет плашки важности: красный — критично, янтарный — важно (§7.4, 1.22)."""
    key = str(severity)
    return SEVERITY_COLOR.get(key, "#64748b")


def qcolor(value: str, *, fallback: str = "#64748b") -> QColor:
    """Цвет из строки: сервер может прислать «чужой» формат — не падаем."""
    color = QColor(str(value or "").strip())
    if not color.isValid():
        color = QColor(fallback)
    return color


#: QSS общих элементов: скругления, hover, disabled. Цвета — из констант выше.
BASE_QSS = f"""
* {{
    font-family: {FONT_FAMILY};
    color: {TEXT};
}}
QLabel[muted="true"] {{
    color: {TEXT_MUTED};
}}
QLabel[title="true"] {{
    font-size: 15px;
    font-weight: 600;
}}
QLabel[role="heading"] {{
    font-size: 13px;
    font-weight: 600;
    color: {TEXT_MUTED};
}}
QPushButton {{
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 7px 14px;
    background: {BG_INPUT};
    font-size: 13px;
}}
QPushButton:hover {{
    background: {BG_CARD_HOVER};
}}
QPushButton:disabled {{
    color: {TEXT_MUTED};
    border-color: rgba(148, 163, 184, 0.12);
}}
QPushButton[accent="true"] {{
    background: {ACCENT};
    border: none;
    color: #0f172a;
    font-weight: 600;
}}
QPushButton[accent="true"]:hover {{
    background: #7dd3fc;
}}
QPushButton[flat="true"] {{
    border: none;
    background: transparent;
    padding: 4px 8px;
}}
QPushButton[flat="true"]:hover {{
    background: {BG_CARD_HOVER};
}}
QTextEdit, QTextBrowser {{
    border: none;
    background: transparent;
    font-size: 13px;
}}
QListWidget {{
    border: none;
    background: transparent;
    outline: none;
}}
QListWidget::item {{
    border-radius: 10px;
    padding: 8px 10px;
    margin: 2px 0;
    color: {TEXT};
}}
QListWidget::item:hover {{
    background: {BG_CARD};
}}
QListWidget::item:selected {{
    background: {BG_CARD_HOVER};
}}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: rgba(148, 163, 184, 0.4);
    border-radius: 4px;
    min-height: 24px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}
"""
