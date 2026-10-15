"""TrayAgent: иконка в трее и показ уведомлений в сессии пользователя (§7.4).

Процесс запускается Планировщиком при входе пользователя (`install_agent.bat`,
Э6) — иначе уведомления не видит никто: служба живёт в Session 0 и окон не
показывает (R3). Иконка — `pystray`, окна — `tkinter`; оба модуля импортируются
**внутри** функций, поэтому CLI работает и без графических библиотек:

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
    TAB_ALL,
    TOAST_SECONDS,
    TrayAction,
    TrayPresenter,
    build_tabs,
    items_for_tab,
    panel_geometry,
)
from client.tray.richtext import (
    KIND_CODE,
    KIND_HEADING,
    KIND_IMAGE,
    KIND_QUOTE,
    KIND_RULE,
    body_blocks,
    outline,
)
from client.tray.session import current_session_id, current_session_user, session_ref
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

#: Заголовок окна и размеры: тост узкий, окно с кнопками — крупнее
ICON_TITLE = "Magaz UI — уведомления"
TOAST_WIDTH = 420
TOAST_HEIGHT = 120
#: Метки уровней в списке окна: сразу видно, что «критично» (§4.6, Э9)
SEVERITY_MARKS: dict[str, str] = {
    str(NotificationSeverity.INFO): "•",
    str(NotificationSeverity.IMPORTANT): "!",
    str(NotificationSeverity.CRITICAL): "‼",
}


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
        """Показать действие: тост, окно — либо строку в консольном режиме."""
        if self.console or self.windows is None:
            self._print_action(action)
            return
        if action.is_toast:
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
        """Кнопка «Ознакомлен»: подтверждение уходит службе и в отчёт (§11.5)."""
        ok = self.client.ack(notification_id, session_ref())
        if ok:
            self.presenter.forget(notification_id)
            self._refresh_unread()
        else:
            logger.warning("Подтверждение уведомления #%s не доставлено", notification_id)
        return ok

    def snooze(self, notification_id: int, *, snooze_min: int | None = None) -> bool:
        """Кнопка «Отложить» (П-2): уведомление остаётся непрочитанным."""
        ok = self.client.snooze(notification_id, session_ref(), snooze_min=snooze_min)
        if ok:
            self.presenter.forget(notification_id)
            self._refresh_unread()
        else:
            logger.warning("Откладывание уведомления #%s не доставлено", notification_id)
        return ok

    def _refresh_unread(self) -> None:
        """Обновить подпись иконки: сколько уведомлений ждёт «Ознакомлен»."""
        if self.windows is not None:
            self.windows.set_unread(self.presenter.visible)

    # --- Цикл ---------------------------------------------------------------

    def _refresh_panel(self) -> None:
        """Обновить окно уведомлений: счётчик, список и «справочник» (§7.4, Э9).

        Справочник читается только когда окно открыто: закрытое окно не должно
        дёргать службу каждые пять секунд, а новые уведомления всё равно придут
        push-ом и откроют окно сами.
        """
        if self.windows is None:
            return
        self.windows.set_unread(self.presenter.visible)
        if not self.windows.is_open:
            return
        try:
            archive = self.client.archive(session_ref())
        except AgentUnreachable as exc:
            logger.debug("Справочник уведомлений недоступен: %s", exc)
            return
        self.presenter.set_archive(archive)
        self.windows.set_archive(archive)

    def run(self) -> int:
        """Основной цикл: иконка в потоке, окна — в главном потоке (§7.4).

        `tkinter` обязан жить в главном потоке, а `pystray` умеет работать в
        фоне, поэтому опрос идёт по расписанию `tkinter`, а иконка — отдельным
        потоком. Так окна и иконка не мешают друг другу и не блокируют опрос.
        """
        self.windows = TrayWindows(
            on_ack=self.acknowledge,
            on_snooze=self.snooze,
            on_quit=self.stop,
            client=self.client,
        )
        icon = _build_icon(
            on_show=lambda: self.windows.show_unread(self.presenter.visible),
            on_quit=self.stop,
        )
        threading.Thread(target=icon.run, name="tray-icon", daemon=True).start()

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
        icon.stop()
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


class TrayWindows:  # pragma: no cover - GUI: проверяется на кассе (Э7)
    """Окно уведомлений в правом верхнем углу кассы (§7.4, Э9).

    Одно окно вместо россыпи мелких: вкладки по важности и «справочник» прочитанного,
    список слева, тело выбранного уведомления справа — текст тегами `tkinter.Text`,
    картинки из локального кэша кассы (Pillow уже в сборке трея, новых зависимостей
    нет). Окно занимает не больше 15 % площади экрана — кассир видит уведомление и
    не теряет рабочее место.

    Живёт в главном потоке (`tkinter` этого требует), решение «что показать» приходит
    из `TrayPresenter`. «Свернуть» убирает окно в трей: уведомления остаются, счётчик
    непрочитанных виден на иконке, а окно открывается из меню и по новому уведомлению.
    """

    def __init__(self, *, on_ack, on_snooze, on_quit, client=None) -> None:  # noqa: ANN001
        import tkinter as tk

        self._tk = tk
        self._on_ack = on_ack
        self._on_snooze = on_snooze
        self._on_quit = on_quit
        #: Клиент кассы нужен только за картинками: они лежат в кэше службы (§7.4)
        self._client = client
        self._root = tk.Tk()
        self._root.withdraw()
        self._window = None
        self._unread: list[NotificationPendingItem] = []
        self._archive: list[NotificationPendingItem] = []
        self._tab = TAB_ALL
        self._selected: int | None = None
        self._title_var = None
        self._tabs_frame = None
        self._listbox = None
        self._body = None
        #: Ссылки на картинки: без них `tkinter` освободит их и покажет пустое место
        self._photos: list = []

    # --- Служебное ----------------------------------------------------------

    def after(self, delay_ms: int, callback) -> None:  # noqa: ANN001 - callback
        """Отложенный вызов в главном потоке (`tkinter`-планировщик)."""
        self._root.after(delay_ms, callback)

    def pump(self) -> None:
        """Запустить цикл обработки окон до закрытия."""
        self._root.mainloop()

    def quit(self) -> None:
        """Остановить цикл (выход из трея)."""
        self._root.quit()

    def destroy(self) -> None:
        """Убрать окна и освободить ресурсы."""
        try:
            self._root.destroy()
        except Exception:  # pragma: no cover - окно могло быть закрыто
            pass

    @property
    def is_open(self) -> bool:
        """Окно показано (не свёрнуто в трей)."""
        window = self._window
        if window is None or not window.winfo_exists():
            return False
        return str(window.state()) != "withdrawn"

    # --- Что показывает окно ------------------------------------------------

    def show(
        self,
        unread: list[NotificationPendingItem],
        archive: list[NotificationPendingItem],
        *,
        focus: int | None = None,
        topmost: bool = False,
    ) -> None:
        """Показать окно: вкладки, список и тело выбранного уведомления (§7.4, Э9)."""
        self._unread = list(unread)
        self._archive = list(archive)
        if focus is not None:
            self._selected = focus
        window = self._ensure_window(topmost=topmost)
        self._render()
        window.deiconify()
        window.lift()

    def set_unread(self, items: list[NotificationPendingItem]) -> None:
        """Незакрытые уведомления сессии: счётчик и список окна (§7.4)."""
        self._unread = list(items)
        if self.is_open:
            self._render()

    def set_archive(self, items: list[NotificationPendingItem]) -> None:
        """«Справочник»: прочитанное, что касса оставляет у себя (§7.4, Э9)."""
        self._archive = list(items)
        if self.is_open:
            self._render()

    def hide(self) -> None:
        """«Свернуть»: окно уходит в трей, уведомления остаются (счётчик на иконке)."""
        if self._window is not None and self._window.winfo_exists():
            self._window.withdraw()

    def toast(self, item: NotificationPendingItem, *, seconds: int = TOAST_SECONDS) -> None:
        """Тост `info`: окно без кнопок, закрывается само через `seconds` (§7.4)."""
        tk = self._tk
        window = tk.Toplevel(self._root)
        window.title(ICON_TITLE)
        x, y, width, height = panel_geometry(
            window.winfo_screenwidth(), window.winfo_screenheight()
        )
        window.geometry(f"{width}x{max(TOAST_HEIGHT, height // 3)}+{x}+{y}")
        window.attributes("-topmost", False)
        tk.Label(window, text=item.title, font=("Segoe UI", 11, "bold"), wraplength=width - 24).pack(
            anchor="w", padx=12, pady=(12, 4)
        )
        tk.Label(
            window,
            text=outline(body_blocks(item.body, item.body_format), limit=160),
            justify="left",
            wraplength=width - 24,
        ).pack(anchor="w", padx=12, pady=(0, 12))
        window.after(seconds * 1000, window.destroy)

    def window(
        self, item: NotificationPendingItem, *, topmost: bool = False, sound: bool = False
    ) -> None:
        """`important`/`critical`: показать окно и выделить это уведомление (§7.4)."""
        if sound:
            self._beep()
        self.show(self._unread, self._archive, focus=item.id, topmost=topmost)

    def show_unread(self, items: list[NotificationPendingItem]) -> None:
        """Пункт меню трея: открыть окно со списком непрочитанных (§7.4)."""
        self.show(items, self._archive)


    # --- Рисование ----------------------------------------------------------

    def _ensure_window(self, *, topmost: bool):
        """Собрать окно один раз; дальше только обновляем содержимое."""
        if self._window is not None and self._window.winfo_exists():
            if topmost:
                self._window.attributes("-topmost", True)
            return self._window
        tk = self._tk
        window = tk.Toplevel(self._root)
        window.title(ICON_TITLE)
        x, y, width, height = panel_geometry(
            window.winfo_screenwidth(), window.winfo_screenheight()
        )
        window.geometry(f"{width}x{height}+{x}+{y}")
        window.attributes("-topmost", bool(topmost))
        window.protocol("WM_DELETE_WINDOW", self.hide)

        header = tk.Frame(window)
        header.pack(fill="x", padx=10, pady=(8, 2))
        self._title_var = tk.StringVar(value=ICON_TITLE)
        tk.Label(header, textvariable=self._title_var, font=("Segoe UI", 11, "bold")).pack(
            side="left"
        )
        tk.Button(header, text="Свернуть", command=self.hide).pack(side="right")
        tk.Button(header, text="Ознакомиться", command=self._acknowledge_selected).pack(
            side="right", padx=4
        )

        self._tabs_frame = tk.Frame(window)
        self._tabs_frame.pack(fill="x", padx=10)

        panes = tk.Frame(window)
        panes.pack(fill="both", expand=True, padx=10, pady=6)
        self._listbox = tk.Listbox(panes, width=26, activestyle="dotbox")
        self._listbox.pack(side="left", fill="y")
        self._listbox.bind("<<ListboxSelect>>", lambda _event: self._select_from_list())
        column = tk.Frame(panes)
        column.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self._body = tk.Text(column, wrap="word", relief="flat", state="disabled")
        self._configure_tags(self._body)
        self._body.pack(fill="both", expand=True)

        footer = tk.Frame(window)
        footer.pack(fill="x", padx=10, pady=(0, 8))
        tk.Button(footer, text="Отложить", command=self._snooze_selected).pack(side="right", padx=4)
        tk.Button(footer, text="Ознакомлен", command=self._acknowledge_selected).pack(side="right")

        self._window = window
        return window

    def _configure_tags(self, widget) -> None:  # noqa: ANN001 - tkinter.Text
        """Теги текста: оформление из разметки (§4.6) — заголовки, списки, цитата."""
        widget.tag_configure("title", font=("Segoe UI", 12, "bold"), spacing3=6)
        widget.tag_configure("heading", font=("Segoe UI", 11, "bold"), spacing3=4)
        widget.tag_configure("focus", font=("Segoe UI", 9), foreground="#b91c1c")
        widget.tag_configure("bold", font=("Segoe UI", 10, "bold"))
        widget.tag_configure("italic", font=("Segoe UI", 10, "italic"))
        widget.tag_configure("underline", underline=True)
        widget.tag_configure("strike", overstrike=True)
        widget.tag_configure("code", font=("Cascadia Mono", 9))
        widget.tag_configure("quote", lmargin1=14, lmargin2=14, foreground="#475569")
        widget.tag_configure("marker", foreground="#475569")

    def _render(self) -> None:
        """Перерисовать окно: заголовок, вкладки, список и тело выбранного."""
        window = self._window
        if window is None or not window.winfo_exists():
            return
        if self._title_var is not None:
            self._title_var.set(f"{ICON_TITLE} — непрочитанных: {len(self._unread)}")

        for child in self._tabs_frame.winfo_children():
            child.destroy()
        for tab in build_tabs(self._unread, self._archive):
            self._tk.Button(
                self._tabs_frame,
                text=f"{tab.title} ({tab.count})",
                relief="sunken" if tab.key == self._tab else "raised",
                command=lambda key=tab.key: self._select_tab(key),
            ).pack(side="left", padx=(0, 4), pady=2)

        items = items_for_tab(self._tab, self._unread, self._archive)
        self._listbox.delete(0, "end")
        for item in items:
            mark = SEVERITY_MARKS.get(str(item.severity), "•")
            self._listbox.insert("end", f"{mark} {item.title}")
        ids = [item.id for item in items]
        if self._selected not in ids:
            self._selected = ids[0] if ids else None
        if self._selected is not None:
            index = ids.index(self._selected)
            self._listbox.selection_clear(0, "end")
            self._listbox.selection_set(index)
            self._listbox.see(index)
        self._render_body(items)

    def _render_body(self, items: list[NotificationPendingItem]) -> None:
        """Тело выбранного уведомления: разметка тегами, картинки из кэша (§4.6, §7.4)."""
        widget = self._body
        if widget is None:
            return
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        self._photos.clear()
        item = next((entry for entry in items if entry.id == self._selected), None)
        if item is None:
            widget.insert(
                "end",
                "Справочник пуст."
                if self._tab == TAB_ARCHIVE
                else "Непрочитанных уведомлений нет.",
            )
            widget.configure(state="disabled")
            return
        widget.insert("end", f"{item.title}\n", "title")
        for block in body_blocks(item.body, item.body_format):
            self._insert_block(widget, block, item.id)
        widget.configure(state="disabled")

    def _insert_block(self, widget, block, notification_id: int) -> None:  # noqa: ANN001
        """Один блок тела: абзац, заголовок, пункт списка, цитата, картинка или линия."""
        if block.kind == KIND_RULE:
            widget.insert("end", "—" * 24 + "\n", "marker")
            return
        if block.kind == KIND_IMAGE:
            self._insert_image(widget, block, notification_id)
            return
        base: list[str] = []
        if block.kind == KIND_HEADING:
            base.append("heading")
        elif block.kind == KIND_QUOTE:
            base.append("quote")
        elif block.kind == KIND_CODE:
            base.append("code")
        if block.marker:
            widget.insert("end", f"{block.marker} ", ("marker",))
        for run in block.runs:
            widget.insert("end", run.text, tuple(base + self._run_tags(widget, run)))
        widget.insert("end", "\n", tuple(base))

    @staticmethod
    def _run_tags(widget, run) -> list[str]:  # noqa: ANN001 - tkinter.Text и TextRun
        """Теги фрагмента: выделения и цвет; цветовые теги создаются по месту."""
        tags: list[str] = []
        if run.bold:
            tags.append("bold")
        if run.italic:
            tags.append("italic")
        if run.underline:
            tags.append("underline")
        if run.strike:
            tags.append("strike")
        if run.code:
            tags.append("code")
        if run.color:
            name = f"fg{run.color}"
            widget.tag_configure(name, foreground=run.color)
            tags.append(name)
        if run.background:
            name = f"bg{run.background}"
            widget.tag_configure(name, background=run.background)
            tags.append(name)
        return tags

    def _insert_image(self, widget, block, notification_id: int) -> None:  # noqa: ANN001
        """Картинка из локального кэша кассы; вместо отсутствующей — подпись (§7.4, Э9)."""
        label = block.alt or block.name
        data = self._client.image(notification_id, block.name) if self._client else None
        photo = _photo_image(data, width=max(160, int(widget.winfo_width()) - 24))
        if photo is None:
            widget.insert("end", f"[{label}]\n", ("marker",))
            return
        self._photos.append(photo)
        widget.insert("end", "\n")
        widget.image_create("end", image=photo, padx=2, pady=4)
        widget.insert("end", "\n")

    # --- Кнопки -------------------------------------------------------------

    def _select_tab(self, key: str) -> None:
        """Переключить вкладку: «все», уровень важности или «справочник»."""
        self._tab = key
        self._selected = None
        self._render()

    def _select_from_list(self) -> None:
        """Пользователь выбрал уведомление в списке — показать его тело."""
        if self._listbox is None:
            return
        selection = self._listbox.curselection()
        items = items_for_tab(self._tab, self._unread, self._archive)
        if not selection or selection[0] >= len(items):
            return
        self._selected = items[selection[0]].id
        self._render_body(items)

    def _acknowledge_selected(self) -> None:
        """«Ознакомлен»: подтверждение уходит службе, уведомление уходит из списка."""
        if self._selected is not None:
            self._on_ack(self._selected)
            self._render()

    def _snooze_selected(self) -> None:
        """«Отложить» (П-2): окно вернётся к пользователю через `snooze_min`."""
        if self._selected is not None:
            self._on_snooze(self._selected)
            self._render()

    def _beep(self) -> None:
        """Звук для `critical`; ошибка звука не должна ломать показ (R2)."""
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONHAND)
        except Exception:  # pragma: no cover - нет звуковой подсистемы
            pass


def _photo_image(data: bytes | None, *, width: int):  # pragma: no cover - GUI
    """Картинка для `tkinter` из байтов кэша: не шире окна, пропорции сохраняются.

    `None` — данных нет: окно покажет подпись `alt` вместо изображения. Масштаб
    считается от ширины окна: схему зала присылают и на 2000 px (§7.4, Э9).
    """
    if not data:
        return None
    try:
        import io

        from PIL import Image, ImageTk

        image = Image.open(io.BytesIO(data))
        if image.width > width:
            height = max(1, int(image.height * width / image.width))
            image = image.resize((width, height))
        return ImageTk.PhotoImage(image)
    except Exception:  # pragma: no cover - битый файл в кэше
        return None


def _build_icon(*, on_show, on_quit):  # pragma: no cover - GUI: проверяется на кассе (Э7)
    """Иконка в трее: меню «Непрочитанные» и «Выход» (`pystray`)."""
    import pystray
    from pystray import Menu, MenuItem

    image = pystray.Icon(ICON_TITLE)
    image.icon = _icon_image()
    image.menu = Menu(
        MenuItem("Непрочитанные уведомления", lambda: on_show()),
        MenuItem("Выход", lambda: on_quit()),
    )
    return image


def _icon_image():  # pragma: no cover - GUI: проверяется на кассе (Э7)
    """Картинка иконки: рисуем сами, чтобы не тащить файл ресурса в exe."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (15, 23, 42, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((14, 24, 50, 44), fill=(226, 232, 240, 255))
    draw.polygon([(14, 24), (50, 24), (32, 10)], fill=(147, 197, 253, 255))
    return image


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
