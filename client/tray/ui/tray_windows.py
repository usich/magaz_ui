"""Адаптер `TrayWindows` на Qt: тот же интерфейс, что был у tkinter (спека 1.22).

`TrayAgent` не должен знать, чем рисуется интерфейс, поэтому здесь сохранены те
же методы: `toast`, `reader`, `window`, `show`, `hide`, `set_unread`,
`set_archive`, `set_archive`, `after`, `pump`, `quit`, `destroy`, `beep`, `is_open`.
Прежняя tkinter-реализация удалена: она осталась только в истории git.

Иконка — `QSystemTrayIcon` (раньше `pystray`): один стек и один цикл событий, а
двойной клик по значку открывает меню уведомлений (требование заказчика).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from client.tray.ui.panel import PanelWindow
from client.tray.ui.reader import ReaderWindow
from client.tray.ui.theme import BASE_QSS
from client.tray.ui.toast import ToastWindow
from shared.schemas import NotificationPendingItem

logger = logging.getLogger(__name__)

__all__ = ["TrayWindows", "build_tray_icon"]

#: Заголовок значка и меню (§7.4)
ICON_TITLE = "Magaz UI — уведомления"


def build_tray_icon(*, on_show: Callable[[], None], on_quit: Callable[[], None]) -> QSystemTrayIcon:
    """Значок в трее: левый клик открывает меню, правый — мини-меню (§7.4, 1.22).

    Иконка рисуется кистью (синий круг со «шляпой» конверта): без файла ресурсов
    дистрибутив остаётся самодостаточным, а значок виден на тёмной и светлой панели.
    """
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#2563eb"))
    painter.drawEllipse(1, 1, 30, 30)
    painter.setBrush(QColor("#e0f2fe"))
    painter.drawRoundedRect(7, 10, 18, 13, 3, 3)
    painter.end()
    icon = QSystemTrayIcon(QIcon(pixmap))
    icon.setToolTip(ICON_TITLE)

    menu = QMenu()
    show_action = QAction("Непрочитанные", menu)
    show_action.triggered.connect(lambda _c=False: on_show())
    quit_action = QAction("Выход", menu)
    quit_action.triggered.connect(lambda _c=False: on_quit())
    menu.addAction(show_action)
    menu.addSeparator()
    menu.addAction(quit_action)
    icon.setContextMenu(menu)

    def clicked(reason: QSystemTrayIcon.ActivationReason) -> None:
        """Двойной клик по значку открывает меню — привычка заказчика (§7.4)."""
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            on_show()

    icon.activated.connect(clicked)
    return icon


class TrayWindows:
    """Окна трея на Qt: тост, читалка и меню (спека 1.22, §7.4).

    Интерфейс повторяет прежнюю tkinter-версию, чтобы `TrayAgent` не менялся.
    Все окна создаются лениво и живут до закрытия; `toast` независим от меню —
    тост всплывает в углу, а меню открывается отдельно (кнопка «Просмотреть» или
    пункт значка).
    """

    def __init__(
        self,
        *,
        on_ack: Callable[[int], None],
        on_snooze: Callable[[int], None],
        on_quit: Callable[[], None],
        client=None,  # noqa: ANN001 - TrayAgentClient, нужен только за картинками
    ) -> None:
        self._on_ack = on_ack
        self._on_snooze = on_snooze
        self._on_quit = on_quit
        self._client = client
        self._unread: list[NotificationPendingItem] = []
        self._archive: list[NotificationPendingItem] = []
        self._toasts: list[ToastWindow] = []
        self._reader: ReaderWindow | None = None
        self._panel: PanelWindow | None = None

    # --- Служебное ----------------------------------------------------------

    def after(self, delay_ms: int, callback: Callable[[], None]) -> None:
        """Отложенный вызов в цикле Qt — замена `tkinter.after` (§7.4)."""
        QTimer.singleShot(int(delay_ms), callback)

    def pump(self) -> None:
        """Запустить цикл событий Qt до остановки (аналог `tkinter.mainloop`)."""
        app = QApplication.instance() or QApplication([])
        app.setStyleSheet(BASE_QSS)
        app.exec()

    def quit(self) -> None:
        """Остановить цикл событий (выход из трея)."""
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def destroy(self) -> None:
        """Закрыть все окна и освободить ресурсы."""
        for toast in list(self._toasts):
            toast.close()
        self._toasts.clear()
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        if self._panel is not None:
            self._panel.close()
            self._panel = None

    @property
    def is_open(self) -> bool:
        """Меню показано (не свёрнуто в трей)."""
        panel = self._panel
        return panel is not None and panel.isVisible()

    # --- Что показывает окно ------------------------------------------------

    def set_unread(self, items: list[NotificationPendingItem]) -> None:
        """Незакрытые уведомления сессии: счётчик и список меню (§7.4)."""
        self._unread = list(items)
        if self._panel is not None:
            self._panel.set_unread(self._unread)

    def set_archive(self, items: list[NotificationPendingItem]) -> None:
        """«Справочник»: прочитанное, что касса оставляет у себя (§7.4, Э9)."""
        self._archive = list(items)
        if self._panel is not None:
            self._panel.set_archive(self._archive)

    def hide(self) -> None:
        """«Свернуть»: меню уходит в трей, уведомления остаются (§7.4)."""
        if self._panel is not None:
            self._panel.hide()

    def show(self, unread: list[NotificationPendingItem], archive: list[NotificationPendingItem], *, focus: int | None = None, topmost: bool = False) -> None:
        """Показать меню со списком; `focus` — выделить уведомление (§7.4)."""
        self._unread = list(unread)
        self._archive = list(archive)
        panel = self._ensure_panel()
        panel.set_unread(self._unread)
        panel.set_archive(self._archive)
        if focus is not None:
            panel._selected = focus  # noqa: SLF001 - намеренно: адресное выделение
        panel.show()
        panel.raise_()
        panel.activateWindow()

    def show_unread(self, items: list[NotificationPendingItem]) -> None:
        """Пункт значка и двойной клик: открыть меню со списком (§7.4, 1.22)."""
        self.show(items, self._archive)

    # --- Окна ---------------------------------------------------------------

    def toast(self, item: NotificationPendingItem, *, seconds: int = 0) -> None:
        """Тост в правом верхнем углу — как уведомление Telegram (§7.4, 1.22).

        Сам не исчезает: закрывается кнопкой «Просмотреть» или «Отложить».
        `seconds` не используется — параметр оставлен для совместимости с
        прежним вызовом из `TrayAgent`.
        """
        _ = seconds
        toast = ToastWindow(item, on_view=self.reader, on_snooze=self._snooze)
        self._toasts.append(toast)
        toast.destroyed.connect(lambda *_: self._drop_toast(toast))
        toast.show()
        toast.move_to_corner()
        toast.raise_()

    def reader(self, item: NotificationPendingItem) -> None:
        """Окно-читалка: полное тело и только «Ознакомлен» / «Отложить» (§7.4, 1.21)."""
        if self._reader is not None:
            self._reader.close()
        reader = ReaderWindow(
            item,
            on_ack=self._ack,
            on_snooze=self._snooze,
            image_provider=self._image,
        )
        self._reader = reader
        reader.show()
        reader.move_to_default()
        reader.raise_()
        reader.activateWindow()

    def window(self, item: NotificationPendingItem, *, topmost: bool = False, sound: bool = False) -> None:
        """Совместимость со старой веткой `KIND_WINDOW`: показать читалку (§7.4)."""
        _ = topmost, sound
        self.reader(item)

    def beep(self) -> None:
        """Звук для `critical`; ошибка звука не должна ломать показ (R2)."""
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONHAND)
        except Exception:  # pragma: no cover - нет звуковой подсистемы
            pass

    # --- Внутреннее ---------------------------------------------------------

    def _drop_toast(self, toast: ToastWindow) -> None:
        """Тост закрылся — убрать его из списка живых окон (утечка ссылок)."""
        if toast in self._toasts:
            self._toasts.remove(toast)

    def _image(self, notification_id: int, name: str) -> bytes | None:
        """Картинка тела из локального кэша кассы; `None` — покажем подпись (§7.4)."""
        if self._client is None:
            return None
        try:
            return self._client.image(notification_id, name)
        except Exception:  # pragma: no cover - защита от сбоя кэша
            logger.debug("Картинка %s уведомления #%s недоступна", name, notification_id)
            return None

    def _ack(self, notification_id: int) -> None:
        """«Ознакомлен»: подтвердить и открыть меню (сценарий заказчика, 1.21)."""
        self._on_ack(notification_id)
        self.show(self._unread, self._archive)

    def _snooze(self, notification_id: int) -> None:
        """«Отложить»: окно просто закрывается, меню не открывается (§7.4, 1.21)."""
        self._on_snooze(notification_id)

    def _ensure_panel(self) -> PanelWindow:
        """Создать меню один раз: дальше только обновляем содержимое (§7.4)."""
        if self._panel is None:
            panel = PanelWindow(
                on_ack=self._on_ack,
                on_snooze=self._on_snooze,
                on_close=lambda: None,
                image_provider=self._image,
            )
            self._panel = panel
        return self._panel

