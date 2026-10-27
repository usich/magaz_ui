"""CLI агента (§7.1): запуск в терминале и служебные команды.

Примеры:

    python -m client.agent --console                          # отладка в терминале
    python -m client.agent run --config C:\\MagazAgent\\agent-config.json
    python -m client.agent info                                # что прочитано из конфига
    python -m client.agent preflight                            # предполётная проверка (Э7)
    python -m client.agent install                              # служба Windows (Э6)
    python -m client.agent start | stop | remove                # управление службой

Команды службы работают через `client/agent/service.py` (`pywin32`) и на
сборочной машине регистрируются скриптом `deploy\\install_agent.bat` (§14.2).
Без `pywin32` CLI печатает подсказку и возвращает код 3 — молча ничего не делает.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from client.agent.app import create_agent_app
from client.agent.config import (
    AGENT_CONFIG_FILE_NAME,
    AgentConfig,
    default_config_path,
    load_config,
)
from client.agent.logging_setup import configure_logging
from client.agent.service import run_service_command, service_available
from shared.version import APP_VERSION

logger = logging.getLogger(__name__)

#: Команды управления службой Windows (§7.1, Э6)
SERVICE_COMMANDS = ("install", "remove", "start", "stop", "debug")
MASK = "***"


def _add_common_options(parser: argparse.ArgumentParser, *, after_subcommand: bool) -> None:
    """Общие ключи агента — одни и те же до подкоманды и после неё (§7.1).

    Живой прогон Э7 показал дефект: `python -m client.agent run --config <файл>`
    падал с «unrecognized arguments», потому что ключи жили только на верхнем
    парсере, а в примерах документации и в инструкции для кассы написано именно
    «после подкоманды». `argument_default=SUPPRESS` у копии для подкоманды не даёт
    ей перезаписать значение, уже разобранное верхним уровнем.
    """

    def default(value: object) -> object:
        return argparse.SUPPRESS if after_subcommand else value

    parser.add_argument(
        "--config", default=default(None), help=f"путь к {AGENT_CONFIG_FILE_NAME} (§7.2)"
    )
    parser.add_argument(
        "--console", action="store_true", default=default(False), help="отладка: лог в терминал"
    )
    parser.add_argument(
        "--log-level", default=default("INFO"), help="уровень логирования (INFO)"
    )
    parser.add_argument(
        "--host", default=default("0.0.0.0"), help="адрес прослушивания (0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=default(None), help="порт (по умолчанию — из конфига, 8383)"
    )


def _build_parser() -> argparse.ArgumentParser:
    commons = argparse.ArgumentParser(add_help=False)
    _add_common_options(commons, after_subcommand=False)
    parser = argparse.ArgumentParser(
        prog="python -m client.agent",
        description="Агент Magaz UI (касса)",
        parents=[commons],
    )
    parser.add_argument("--version", action="version", version=f"MagazAgent {APP_VERSION}")

    sub = parser.add_subparsers(dest="command")
    # `--config` и прочие ключи принимаются и после подкоманды: так их пишут в
    # примерах и в инструкции для кассы, поэтому копия тех же ключей есть у каждой
    uses_common = argparse.ArgumentParser(add_help=False)
    _add_common_options(uses_common, after_subcommand=True)

    sub.add_parser("run", parents=[uses_common], help="запустить агент (uvicorn)")
    sub.add_parser("info", parents=[uses_common], help="показать, что прочитано из конфига")

    preflight = sub.add_parser(
        "preflight", parents=[uses_common], help="проверить готовность кассы к приёмке (Э7)"
    )
    preflight.add_argument("--json", action="store_true", help="отчёт в JSON — для протокола")
    preflight.add_argument(
        "--no-server", action="store_true", help="не проверять доступность сервера"
    )
    for name, help_text in (
        ("install", "установить службу Windows (Э6)"),
        ("remove", "удалить службу Windows (Э6)"),
        ("start", "запустить службу (Э6)"),
        ("stop", "остановить службу (Э6)"),
        ("debug", "отладка службы (Э6)"),
    ):
        sub.add_parser(name, parents=[uses_common], help=help_text)
    return parser


def _cmd_service(command: str, argv: list[str] | None = None) -> int:
    """Управление службой Windows; `--console` остаётся способом отладки (§7.5)."""
    if not service_available():
        print(
            f"Команда {command!r} недоступна: это не Windows или не установлен pywin32 "
            "(см. docs\\INSTALL.md). Для отладки используйте --console."
        )
    return run_service_command(command, argv)


def _mask(value: str | None) -> str:
    """Маска секрета: значения токена и пароля в вывод не попадают (I-7)."""
    return MASK if value else "—"


def _cmd_info(config: AgentConfig, config_path: str | None) -> int:
    """Показать разобранный конфиг — первое, что смотрят при диагностике."""
    print(f"MagazAgent {APP_VERSION}")
    print(f"Конфиг:        {config_path or default_config_path()}")
    print(f"Магазин:       id={config.store_id} {config.store_name!r}")
    print(f"Сервер:        {config.server_url or '—'}")
    print(f"Токен агента:  {_mask(config.agent_token)}")
    print(f"Порт:          {config.port}")
    print(f"Рабочая папка: {config.work_path}")
    print(f"Платформа 1С:  {config.base_1c_path} (версия — из store.version_1c)")
    print(f"Файл обновл.:  {config.default_file_name}")
    print(
        f"Профиль 1С:    {config.one_c_profile.user_name or '—'} "
        f"(версия {config.credentials_version}, пароль {_mask(config.one_c_profile.password_enc)})"
    )
    print(f"Устройства:    версия списка {config.devices_version} (Д-1)")
    print(f"Состояние:     {config.state_path} (текущая операция, §7.3)")
    print(f"Логи:          {config.log_dir}")
    return 0


def _cmd_preflight(args: argparse.Namespace) -> int:
    """Предполётная проверка кассы (Э7): отчёт прикладывается к протоколу приёмки.

    Конфиг читается внутри проверки, а не в `main`: если файла нет, отчёт должен
    объяснить это одной строкой, а не упасть с ошибкой загрузки (§7.2).
    """
    from client.agent.preflight import render_preflight, run_preflight

    report = run_preflight(config_path=args.config, check_server=not args.no_server)
    print(render_preflight(report, as_json=args.json))
    return report.exit_code


def _cmd_run(args: argparse.Namespace, config: AgentConfig, config_path: str | None) -> int:
    """Запустить агент (uvicorn). `--console` включается флагом, а не отдельной командой.

    Путь конфига передаётся в приложение **всегда**, когда файл существует: по
    этому пути агент сохраняет профиль 1С, доставленный push-ом `POST /credentials`
    (П-1). Иначе после перезапуска службы пароль пришлось бы ждать снова — конфиг
    читается из `--config`, переменной `MAGAZ_AGENT_CONFIG` или рабочей папки (§7.1).
    """
    import uvicorn

    log_file = configure_logging(config, console=args.console, level=args.log_level)
    port = args.port or config.port
    resolved_config = Path(config_path) if config_path else default_config_path()
    application = create_agent_app(
        config, config_path=resolved_config if resolved_config.is_file() else None
    )
    logger.info(
        "MagazAgent стартует: магазин id=%s, порт %s, рабочая папка %s, лог %s",
        config.store_id,
        port,
        config.work_path,
        log_file or "консоль",
    )
    uvicorn.run(application, host=args.host, port=port, log_level=args.log_level.lower())
    return 0


def _run_as_service() -> int:
    """Запустить процесс как службу Windows (вызывается из `main` при `-service`).

    Служба запускает `magaz-agent.exe -service` (см. `_exe_args_` в `service.py`):
    в этом режиме exe не запускает uvicorn как консольное приложение, а передаёт
    управление диспетчеру служб, который вызывает `SvcDoRun` и отправляет SCM
    статусы (иначе SCM ждал бы `SERVICE_RUNNING` и падал с ошибкой 1053, хотя
    uvicorn уже отвечал на 8383).
    """
    import servicemanager

    from client.agent.service import build_service_class

    servicemanager.Initialize()
    # В pywin32 метод называется `PrepareToHostSingle` (PrepareServiceHosting
    # не существует) — регистрируем класс службы, после чего диспетчер служб
    # вызывает SvcDoRun и шлёт SCM статусы.
    servicemanager.PrepareToHostSingle(build_service_class())
    servicemanager.StartServiceCtrlDispatcher()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI (без аргументов — `run` в терминале, как `--console`)."""
    # `parse_known_args`, а не `parse_args`: скрипты установки передают службе
    # `install --startup auto` (§14.2), и эти аргументы должен разобрать
    # `win32serviceutil`, а не argparse CLI (иначе «unrecognized arguments»).
    args, _extra = _build_parser().parse_known_args(argv)
    command = args.command or "run"

    # Служба Windows запускает exe с `-service`/`--service`: это НЕ подкоманда
    # CLI, а признак «стать процессом службы» (см. `_run_as_service`).
    if "-service" in _extra or "--service" in _extra:
        return _run_as_service()

    if command in SERVICE_COMMANDS:
        return _cmd_service(command, [sys.argv[0], *sys.argv[2:]])

    if command == "preflight":
        return _cmd_preflight(args)

    try:
        config = load_config(args.config)
    except FileNotFoundError as exc:
        print(f"Ошибка: {exc}")
        return 2

    if command == "info":
        return _cmd_info(config, args.config)
    return _cmd_run(args, config, args.config)


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    raise SystemExit(main())
