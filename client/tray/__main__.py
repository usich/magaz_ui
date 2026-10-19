"""TrayAgent: иконка в трее и показ уведомлений в сессии пользователя (§7.4).

Процесс запускается Планировщиком при входе пользователя (`install_agent.bat`,
Э6) — иначе уведомления не видит никто: служба живёт в Session 0 и окон не
показывает (R3).

Спека 1.22: интерфейс рисуется на **Qt (PySide6)** — тёмные «стеклянные» окна с
настоящим размытием фона (acrylic) и скруглениями; иконка — `QSystemTrayIcon`.
Прежняя пара `pystray` + `tkinter` удалена: она давала «деревянные» окна без
полупрозрачности, а её дублирующий канал показа через `msg.exe` заменён тостом.

    MagazTrayAgent.exe --console          # показывать действия текстом (отладка)
    MagazTrayAgent.exe --pending          # что сейчас ждёт ознакомления, и выйти
    MagazTrayAgent.exe --once             # один цикл опроса (диагностика на кассе)
    MagazTrayAgent.exe --ack 12           # отправить «Ознакомлен» из этой сессии
    MagazTrayAgent.exe --snooze 12        # отправить «Отложить» (П-2, Д-10)

Токена агента у трея нет: он знает только `127.0.0.1:8383` (§7.4).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

from client.agent.config import LOG_DIR_ENV
from client.tray.api import DEFAULT_AGENT_URL, AgentUnreachable, TrayAgentClient
from client.tray.presenter import (
    TOAST_SECONDS,
    TrayAction,
    TrayPresenter,
)
from client.tray.session import current_session_id, current_session_user, session_ref
from client.tray.ui.tray_windows import ICON_TITLE, TrayWindows, build_tray_icon
from shared.enums import NotificationSeverity
from shared.schemas import NotificationPendingItem
from shared.version import APP_VERSION

logger = logging.getLogger(__name__)

__all__ = ["TrayAgent", "build_parser", "configure_tray_logging", "main"]

#: Период опроса локальной службы, с. Трей — не мониторинг: важна реакция на
#: уведомление, а не частота; 5 с даёт задержку, незаметную для кассира.
DEFAULT_INTERVAL_SEC = 5.0

#: Логи трея лежат рядом с логами службы (§14.4): у окна трея консоли нет
TRAY_LOG_FILE_NAME = "tray.log"
DEFAULT_LOG_DIR = r"C:\ProgramData\MagazAgent\logs"


def configure_tray_logging(*, console: bool, level: str = "INFO") -> Path | None:
    """Настроить логи трея: в консоль (`--console`) или в файл (§14.4).

    Файл обязателен для обычного режима: exe собран без консоли (`tray.spec`),
    поэтому иначе о «почему уведомление не показалось» узнать неоткуда.
    Каталог логов можно переопределить переменной `MAGAZ_AGENT_LOG_DIR` — той
    же, что использует служба (§7.1).
    """
    log_level = getattr(logging, str(level).upper(), logging.INFO)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if console:
        logging.basicConfig(level=log_level, format=fmt, stream=sys.stdout, force=True)
        return None

    log_dir = Path(os.environ.get(LOG_DIR_ENV, DEFAULT_LOG_DIR))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / TRAY_LOG_FILE_NAME
    logging.basicConfig(
        level=log_level, format=fmt, filename=log_path, encoding="utf-8", force=True
    )
    return log_path



class TrayAgent:
    """Цикл «опросить службу → показать → записать подтверждение».

    Класс держит состояние показа (`TrayPresenter`) и клиент локальной службы;
    всё, что касается пикселей, вынесено в `TrayWindows`, поэтому `--console`
    и `--once` проходят через ту же логику, что и обычный режим трея.
    """

    def __init__(
        self,
        client: TrayAgentClient,
        *,
        interval_sec: float = DEFAULT_INTERVAL_SEC,
        presenter: TrayPresenter | None = None,
        console: bool = False,
        windows: "TrayWindows | None" = None,
    ) -> None:
        self.client = client
        self.interval_sec = interval_sec
        self.presenter = presenter or TrayPresenter()
        self.console = console
        #: Значок в трее (QSystemTrayIcon) — создаётся в `run()` (§7.4, 1.22)
        self._icon = None
        self.windows = windows
        self._stop = threading.Event()

    # --- Один цикл ----------------------------------------------------------

    def poll_once(self) -> list[TrayAction]:
        """Опросить службу и выполнить действия; возвращает показанное.

        Недоступность службы не ошибка: касса могла перезапускаться. Трей
        молча ждёт следующего цикла — уведомления лежат в очереди на диске.
        """
        try:
            items = self.client.pending(session_ref())
        except AgentUnreachable as exc:
            logger.debug("Служба агента недоступна: %s", exc)
            return []

        actions = self.presenter.decide(items)
        for action in actions:
            self.handle(action)
        self._refresh_panel()
        return actions

    def handle(self, action: TrayAction) -> None:
        """Показать действие: тост с кнопками — либо строку в консольном режиме.

        С 1.21 трей сам окна не открывает: и «важное», и «критичное» сначала
        всплывает тостом, а читалку открывает кнопка «Просмотреть». Ветка
        `KIND_WINDOW` оставлена как совместимость (старые записи и вызовы).
        """
        if self.console or self.windows is None:
            self._print_action(action)
            return
        if action.is_toast:
            if str(action.notification.severity) == str(NotificationSeverity.CRITICAL):
                # Критичное не проспать: с 1.21 повтор тоже всплывает тостом
                self.windows.beep()
            self.windows.toast(action.notification, seconds=TOAST_SECONDS)
            # Тост считается показанным: `shown` фиксируется сразу (§7.4)
            self.acknowledge_shown(action.notification.id)
            return
        critical = str(action.notification.severity) == "critical"
        self.windows.window(action.notification, topmost=critical, sound=critical)

    def _print_action(self, action: TrayAction) -> None:
        """Консольный режим: то же решение, но текстом (и без записи `shown`)."""
        prefix = "тост" if action.is_toast else "окно"
        item = action.notification
        repeat = f", повтор каждые {item.repeat_every_min} мин" if item.repeat_every_min else ""
        print(f"[{prefix}] #{item.id} [{item.severity}] {item.title}{repeat}")

    # --- Подтверждения ------------------------------------------------------

    def acknowledge_shown(self, notification_id: int) -> bool:
        """`shown` для тоста: без него сервер не узнает, что уведомление видели."""
        ok = self.client.shown(notification_id, session_ref())
        if not ok:
            logger.debug("Не удалось отметить показ уведомления #%s", notification_id)
        return ok

    def acknowledge(self, notification_id: int) -> bool:
        """Кнопка «Ознакомлен»: подтверждение уходит службе и в отчёт (§11.5).

        После успеха справочник читается заново (§7.4, 1.21-fix): уведомление с
        `delete_after_read=false` обязано появиться на вкладке «справочник», а
        без свежего `archive()` окно осталось бы со старой копией.
        """
        ok = self.client.ack(notification_id, session_ref())
        if ok:
            self.presenter.forget(notification_id)
            self._refresh_panel()
        else:
            logger.warning("Подтверждение уведомления #%s не доставлено", notification_id)
        return ok

    def snooze(self, notification_id: int, *, snooze_min: int | None = None) -> bool:
        """Кнопка «Отложить» (П-2): уведомление остаётся непрочитанным.

        Архив обновляется так же, как при «Ознакомлен»: отложенное уведомление
        уходит из непрочитанных, но остаётся в списке для окна.
        """
        ok = self.client.snooze(notification_id, session_ref(), snooze_min=snooze_min)
        if ok:
            self.presenter.forget(notification_id)
            self._refresh_panel()
        else:
            logger.warning("Откладывание уведомления #%s не доставлено", notification_id)
        return ok

    # --- Цикл ---------------------------------------------------------------

    def _refresh_panel(self) -> None:
        """Обновить окно уведомлений: счётчик, список и «справочник» (§7.4, Э9).

        Архив читается **всегда**, а не только при открытом окне: меню категорий
        открывается сразу после «Ознакомлен» из читалки и обязано показать свежий
        справочник с только что прочитанным уведомлением (`delete_after_read=false`).
        Запрос недорогой — один GET к локальной службе, а окно закрытое просто не
        перерисовывается (`set_archive` в `TrayWindows` сам это проверяет).
        """
        if self.windows is None:
            return
        self.windows.set_unread(self.presenter.visible)
        try:
            archive = self.client.archive(session_ref())
        except AgentUnreachable as exc:
            logger.debug("Справочник уведомлений недоступен: %s", exc)
            return
        self.presenter.set_archive(archive)
        self.windows.set_archive(archive)

    def run(self) -> int:
        """Основной цикл: иконка и окна в одном цикле событий Qt (§7.4, 1.22).

        Раньше опрос шёл по расписанию `tkinter`, а `pystray` жил в отдельном
        потоке. Теперь весь интерфейс — Qt: иконка `QSystemTrayIcon` и окна делят
        один цикл событий, а опрос идёт по таймеру `windows.after(...)`.
        """
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        app.setApplicationName(ICON_TITLE)
        # Закрытие последнего окна не завершает приложение: трей живёт в значке
        app.setQuitOnLastWindowClosed(False)

        self.windows = TrayWindows(
            on_ack=self.acknowledge,
            on_snooze=self.snooze,
            on_quit=self.stop,
            client=self.client,
        )
        icon = build_tray_icon(
            on_show=lambda: self.windows.show_unread(self.presenter.visible),
            on_quit=self.stop,
        )
        icon.show()
        self._icon = icon

        def tick() -> None:
            try:
                if self._stop.is_set():
                    self.windows.quit()
                    return
                self.poll_once()
            finally:
                if not self._stop.is_set():
                    self.windows.after(int(self.interval_sec * 1000), tick)

        logger.info(
            "TrayAgent запущен: сессия %s, пользователь %s, опрос каждые %.0f с",
            current_session_id(),
            current_session_user(),
            self.interval_sec,
        )
        self.windows.after(0, tick)
        self.windows.pump()
        icon.hide()
        return 0

    def stop(self) -> None:
        """Остановить цикл (меню трея «Выход» или завершение сеанса)."""
        self._stop.set()

    @property
    def stopped(self) -> bool:
        """Пора ли выходить из цикла (`--console` проверяет это в каждом круге)."""
        return self._stop.is_set()

    def close(self) -> None:
        """Закрыть клиент и окна."""
        self.client.close()
        if self.windows is not None:
            self.windows.destroy()


# Окна трея живут в отдельном модуле на Qt (спека 1.22):
# client.tray.ui.tray_windows.TrayWindows повторяет прежний интерфейс
# (toast, reader, show, hide, set_unread, set_archive, after, pump, quit,
# destroy, is_open, beep), поэтому TrayAgent выше не меняется. Прежняя
# tkinter-реализация удалена: она рисовала «деревянные» окна без размытия
# и скруглений, а заказчик просил «жидкое стекло» (acrylic + blur).
def build_parser() -> argparse.ArgumentParser:
    """Разобрать ключи CLI (§7.5: обязательный режим отладки `--console`)."""
    parser = argparse.ArgumentParser(
        prog="MagazTrayAgent", description="Показ уведомлений в сессии пользователя (§7.4)"
    )
    parser.add_argument("--version", action="version", version=f"MagazTrayAgent {APP_VERSION}")
    parser.add_argument(
        "--agent-url",
        default=DEFAULT_AGENT_URL,
        help=f"адрес локальной службы ({DEFAULT_AGENT_URL})",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SEC,
        help=f"период опроса службы, с ({DEFAULT_INTERVAL_SEC:g})",
    )
    parser.add_argument(
        "--console", action="store_true", help="показывать действия текстом, без окон"
    )
    parser.add_argument("--once", action="store_true", help="один цикл опроса и выход")
    parser.add_argument("--pending", action="store_true", help="показать список и выйти")
    parser.add_argument("--ack", type=int, help="отправить «Ознакомлен» для уведомления")
    parser.add_argument("--snooze", type=int, help="отправить «Отложить» (П-2, Д-10)")
    parser.add_argument("--log-level", default="INFO", help="уровень логирования (INFO)")
    return parser


def _print_pending(items: list[NotificationPendingItem]) -> None:
    """Список непрочитанных для диагностики: id, уровень, заголовок, срок возврата."""
    if not items:
        print("Непрочитанных уведомлений нет")
        return
    for item in items:
        extra = f" (вернётся после {item.snooze_until})" if item.snooze_until else ""
        print(f"#{item.id} [{item.severity}] {item.title}{extra}")


def main(argv: list[str] | None = None) -> int:
    """Точка входа TrayAgent (§7.4, §7.5).

    Коды возврата: `0` — успех, `2` — служба недоступна или подтверждение не
    доставлено (для скриптов и Планировщика видно, что что-то не так).
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    client = TrayAgentClient(args.agent_url)
    agent = TrayAgent(client, interval_sec=args.interval, console=args.console)
    try:
        if args.pending:
            try:
                _print_pending(client.pending(session_ref()))
            except AgentUnreachable as exc:
                print(f"Служба агента недоступна: {exc}")
                return 2
            return 0

        if args.ack is not None:
            return 0 if agent.acknowledge(args.ack) else 2

        if args.snooze is not None:
            return 0 if agent.snooze(args.snooze) else 2

        if args.once:
            actions = agent.poll_once()
            if not actions:
                print("Показывать нечего")
            return 0

        if args.console:
            configure_tray_logging(console=True, level=args.log_level)
            print("Консольный режим: окна не показываются, Ctrl+C — выход")
            while not agent.stopped:
                agent.poll_once()
                time.sleep(args.interval)
            return 0

        # Обычный режим: exe собран без консоли, поэтому логи идут в файл (§14.4)
        log_path = configure_tray_logging(console=False, level=args.log_level)
        logger.info("Логи трея: %s", log_path)
        return agent.run()
    except KeyboardInterrupt:  # pragma: no cover - ручной запуск
        return 0
    finally:
        agent.close()


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    sys.exit(main())
