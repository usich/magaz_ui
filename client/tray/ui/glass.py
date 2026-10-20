"""Тёмное «жидкое стекло»: DWM-acrylic, скругления и общие элементы (спека 1.22).

Размытие фона и скругления делает сама Windows — Qt только просит об этом через
`DwmSetWindowAttribute`. Это важно: «имитация стекла» кистью выглядит плоско, а
настоящий acrylic показывает под панелью размытый рабочий стол и окна 1С.

Модуль намеренно не зависит от остального трея: чистые функции `enable_backdrop`
и `disable_rounding` можно проверить без окна, а `GlassWindow` — тонкий миксин.
Вне Windows функции возвращают `False` и ничего не ломают (деградация, а не
падение: тесты и разработка идут и на других системах).
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

logger = logging.getLogger(__name__)

__all__ = [
    "DWMSBT_MAINWINDOW",
    "DWMSBT_TRANSIENTWINDOW",
    "PANEL_RADIUS",
    "TOAST_RADIUS",
    "GlassWindow",
    "apply_dwm_glass",
    "is_windows",
]

#: Значения атрибутов DWM (Windows 11): тёмный режим, скругления, тип подложки
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
#: `DWMSBT_TRANSIENTWINDOW` — acrylic (как у всплывающих панелей Windows);
#: `DWMSBT_MAINWINDOW` — mica (мягче, подходит большим окнам).
DWMSBT_TRANSIENTWINDOW = 3
DWMSBT_MAINWINDOW = 2
#: Скругления углов средствами Windows (иначе Qt-окно будет прямоугольным)
DWMWCP_ROUND = 2

#: Радиус скругления тоста и меню — согласован с тем, что рисует Qt-кисть
TOAST_RADIUS = 16
PANEL_RADIUS = 20

#: Цвет «стеклянного» слоя поверх blur: тёмная синева с непрозрачностью.
#: 1.22: непрозрачность стекла у всех окон — 90 % (alpha 230), как запросил
#: заказчик: фон почти плотный, разметка и текст читаются отлично поверх blur.
GLASS_TINT = QColor(15, 23, 42, 230)
#: Цвет слоя у больших окон: тот же тон и та же прозрачность — все окна одинаково
GLASS_TINT_DENSE = QColor(15, 23, 42, 230)


def is_windows() -> bool:
    """Платформа Windows: только там есть DWM и acrylic (деградация вне её)."""
    return sys.platform == "win32"


def apply_dwm_glass(
    hwnd: int,
    *,
    backdrop: int = DWMSBT_TRANSIENTWINDOW,
    dark: bool = True,
    rounded: bool = True,
) -> bool:
    """Включить тёмный режим, скругления и acrylic-подложку окна (спека 1.22).

    Возвращает `True`, если Windows приняла запрос. Вне Windows — `False` без
    исключения: окно просто не получит размытия, но останется работоспособным
    (важная часть деградации — приложение не падает на стенде без DWM).

    `hwnd` — целое из `int(window.winId())`: окно должно быть создано, иначе
    Windows вернёт ошибку и стекла не будет.
    """
    if not is_windows():
        return False
    try:
        dwmapi = ctypes.windll.dwmapi  # type: ignore[attr-defined]
    except (AttributeError, OSError):  # pragma: no cover - урезанная Windows
        logger.debug("dwmapi недоступен: «жидкое стекло» не включить")
        return False

    handle = wintypes.HWND(int(hwnd))
    results: list[int] = []

    def set_attr(attribute: int, value: int) -> None:
        raw = ctypes.c_int(value)
        results.append(
            int(dwmapi.DwmSetWindowAttribute(handle, attribute, ctypes.byref(raw), 4))
        )

    if dark:
        set_attr(DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
    if rounded:
        set_attr(DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
    set_attr(DWMWA_SYSTEMBACKDROP_TYPE, int(backdrop))

    ok = all(code == 0 for code in results) and bool(results)
    if not ok:
        # Старые сборки Windows 10 не знают `SYSTEMBACKDROP_TYPE`: это не ошибка —
        # панель останется тёмной, просто без размытия фона (§7.4, ограничение)
        logger.debug("DWM-подложка не применена целиком: %s", results)
    return ok


class GlassWindow(QWidget):
    """Основа «стеклянных» окон: прозрачный фон, blur под ним, свои скругления.

    Окно без системной рамки (`FramelessWindowHint`) всегда поверх остальных
    (`WindowStaysOnTopHint`): тост обязан быть виден поверх полноэкранной 1С.

    `WA_TranslucentBackground` нужен, чтобы Qt не заливал фон: тогда под
    полупрозрачной кистью виден размытый рабочий стол (acrylic). Слой стекла
    рисуется в `paintEvent`: Qt не умеет «круглить» само окно, поэтому рисуем
    скруглённый прямоугольник кистью.
    """

    #: Радиус скругления этого окна (потомки переопределяют)
    radius: int = PANEL_RADIUS
    #: Цвет стеклянного слоя (потомки могут сделать плотнее)
    tint: QColor = GLASS_TINT
    #: Тип подложки DWM: acrylic для тоста, mica для больших окон
    backdrop: int = DWMSBT_TRANSIENTWINDOW

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._glass_applied = False

    def showEvent(self, event) -> None:  # noqa: ANN001 - Qt
        """Применить стекло к HWND — только после создания окна (спека 1.22)."""
        super().showEvent(event)
        if self._glass_applied:
            return
        # winId() создаёт HWND; до этого DWM-атрибуты ставить некуда
        self._glass_applied = True
        apply_dwm_glass(int(self.winId()), backdrop=self.backdrop)

    def paintEvent(self, event) -> None:  # noqa: ANN001 - Qt
        """Нарисовать скруглённый полупрозрачный слой — «стекло» поверх blur."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.tint)
        rect = self.rect().adjusted(1, 1, -1, -1)
        radius = float(self.radius)
        painter.drawRoundedRect(rect, radius, radius)
