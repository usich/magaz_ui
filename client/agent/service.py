"""Служба Windows `MagazAgent` (§7.1, Э6): автозапуск без входа пользователя.

Служба — это тот же агент (`create_agent_app` + `uvicorn`), но запущенный
диспетчером служб в Session 0 под `LocalSystem`. Отсюда два следствия, которые
нельзя потерять:

* **окна показать нельзя** (R3): показ уведомлений делает TrayAgent в сессии
  пользователя, а служба лишь дублирует важное через `msg.exe` (§7.4);
* **пути вычисляются от места запуска exe** (I-14): конфиг служба берёт из
  `agent-config.json` рядом с exe или из `MAGAZ_AGENT_CONFIG` (§7.1), поэтому
  `install_agent.bat` не передаёт никаких путей.

`pywin32` нужен только для регистрации и управления службой, поэтому
импортируется **внутри** функции: на машине разработки без `pywin32` модуль
импортируется, а команды `install`/`start`/`stop`/`remove` печатают понятный
совет вместо трейсбека (§7.5: «иначе на кассе нечего смотреть»).
"""

from __future__ import annotations

import logging
import sys

__all__ = [
    "PYWIN32_HINT",
    "SERVICE_DESCRIPTION",
    "SERVICE_DISPLAY_NAME",
    "SERVICE_NAME",
    "build_service_class",
    "run_service_command",
    "service_available",
]

logger = logging.getLogger(__name__)

#: Имена службы — из §7.1 и §14.2: их же использует `install_agent.bat`
SERVICE_NAME = "MagazAgent"
SERVICE_DISPLAY_NAME = "Magaz UI — агент кассы"
SERVICE_DESCRIPTION = (
    "Агент обновления 1С (Magaz UI): операции слияния и обновления, "
    "опрос оборудования подсети, очередь уведомлений (§7.1)"
)

#: Подсказка, если `pywin32` не установлен (сборочная машина ставит его из
#: `requirements-build.txt`; на кассе он приходит внутри exe)
PYWIN32_HINT = (
    "Для управления службой Windows нужен pywin32: "
    "pip install -r requirements-build.txt (см. docs\\INSTALL.md)"
)

#: Код возврата CLI, когда команда недоступна в текущем окружении
UNSUPPORTED_CODE = 3


def service_available() -> bool:
    """Есть ли `pywin32`: без него службу не зарегистрировать и не запустить."""
    try:
        import win32serviceutil  # noqa: F401
    except ImportError:
        return False
    return True


def build_service_class():  # noqa: ANN201 - класс создаётся динамически
    """Собрать класс службы для `win32serviceutil` (импорт `pywin32` — ленивый).

    Класс определяется внутри функции, чтобы модуль импортировался на любой
    машине: `pytest` и `info` не зависят от наличия службы.
    """
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    from client.agent.app import create_agent_app
    from client.agent.config import load_config
    from client.agent.logging_setup import configure_logging

    class MagazAgentService(win32serviceutil.ServiceFramework):
        """Служба `MagazAgent` (§7.1): тот же агент, но под диспетчером служб."""

        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args) -> None:  # noqa: ANN001 - аргументы SCM
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.server = None

        def SvcStop(self) -> None:
            """Диагностика службы остановилась — просим uvicorn завершиться."""
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)
            if self.server is not None:
                self.server.should_exit = True

        def SvcDoRun(self) -> None:
            """Точка входа службы: поднять агент и держать его до остановки."""
            servicemanager.LogInfoMsg(f"{SERVICE_NAME}: запуск")
            try:
                self._serve()
            except Exception as exc:  # pragma: no cover - сбой только на кассе
                servicemanager.LogErrorMsg(f"{SERVICE_NAME}: ошибка — {exc!r}")
                raise
            finally:
                servicemanager.LogInfoMsg(f"{SERVICE_NAME}: остановлен")

        def _serve(self) -> None:  # pragma: no cover - проверяется на кассе (Э7)
            """Поднять uvicorn в процессе службы (§14.2).

            Логи пишутся в `C:\\ProgramData\\MagazAgent\\logs\\agent.log`
            (`configure_logging`), а не в консоль: у службы консоли нет.
            """
            import uvicorn

            config = load_config()
            configure_logging(config, console=False)
            application = create_agent_app(config, config_path=None)
            self.server = uvicorn.Server(
                uvicorn.Config(
                    application,
                    host="0.0.0.0",
                    port=config.port,
                    log_level="info",
                )
            )
            logger.info(
                "Служба %s обслуживает магазин id=%s на порту %s",
                SERVICE_NAME,
                config.store_id,
                config.port,
            )
            self.server.run()

    return MagazAgentService


def run_service_command(command: str, argv: list[str] | None = None) -> int:
    """`install`/`remove`/`start`/`stop`/`debug` через `win32serviceutil` (§14.2).

    Возвращает код возврата CLI; без `pywin32` печатает подсказку и возвращает
    `UNSUPPORTED_CODE`, чтобы скрипт установки увидел ненулевой код и остановился.
    """
    if not service_available():
        print(PYWIN32_HINT)
        return UNSUPPORTED_CODE

    import win32serviceutil

    arguments = list(argv or sys.argv)
    # `HandleCommandLine` ждёт argv вида [`<exe>`, `<команда>`] — остальное он
    # разберёт сам (`--startup auto` и прочее добавят скрипты установки)
    handle_argv = [arguments[0], command]
    win32serviceutil.HandleCommandLine(build_service_class(), argv=handle_argv)
    logger.info("Команда службы %s выполнена: %s", SERVICE_NAME, command)
    return 0
