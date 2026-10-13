"""CLI агента (§7.1): запуск в терминале и служебные команды.

Примеры:

    python -m client.agent --console                          # отладка в терминале
    python -m client.agent run --config C:\\MagazAgent\\agent-config.json
    python -m client.agent info                                # что прочитано из конфига

Команды службы (`install`/`remove`/`start`/`stop`/`debug`) появятся на этапе Э6
вместе с pywin32-обёрткой, PyInstaller-спеком и `install_agent.bat` (§12) —
до этого CLI честно сообщает, что команда ещё не реализована.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from client.agent.app import create_agent_app
from client.agent.config import (
    AGENT_CONFIG_FILE_NAME,
    AgentConfig,
    default_config_path,
    load_config,
)
from client.agent.logging_setup import configure_logging
from shared.version import APP_VERSION

logger = logging.getLogger(__name__)

#: Команды, которые появляются на Э6 (управление службой Windows)
SERVICE_COMMANDS = ("install", "remove", "start", "stop", "debug")
MASK = "***"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m client.agent", description="Агент Magaz UI (касса)"
    )
    parser.add_argument("--version", action="version", version=f"MagazAgent {APP_VERSION}")
    parser.add_argument("--config", help=f"путь к {AGENT_CONFIG_FILE_NAME} (§7.2)")
    parser.add_argument("--console", action="store_true", help="отладка: лог в терминал")
    parser.add_argument("--log-level", default="INFO", help="уровень логирования (INFO)")
    parser.add_argument("--host", default="0.0.0.0", help="адрес прослушивания (0.0.0.0)")
    parser.add_argument("--port", type=int, help="порт (по умолчанию — из конфига, 8383)")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="запустить агент (uvicorn)")
    sub.add_parser("info", help="показать, что прочитано из конфига")
    for name, help_text in (
        ("install", "установить службу Windows (Э6)"),
        ("remove", "удалить службу Windows (Э6)"),
        ("start", "запустить службу (Э6)"),
        ("stop", "остановить службу (Э6)"),
        ("debug", "отладка службы (Э6)"),
    ):
        sub.add_parser(name, help=help_text)
    return parser


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


def _cmd_run(args: argparse.Namespace, config: AgentConfig, config_path: str | None) -> int:
    """Запустить агент (uvicorn). `--console` включается флагом, а не отдельной командой."""
    import uvicorn

    log_file = configure_logging(config, console=args.console, level=args.log_level)
    port = args.port or config.port
    application = create_agent_app(
        config, config_path=Path(config_path) if config_path else None
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


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI (без аргументов — `run` в терминале, как `--console`)."""
    args = _build_parser().parse_args(argv)
    command = args.command or "run"

    if command in SERVICE_COMMANDS:
        print(
            f"Команда {command!r} появится на этапе Э6 (§12): "
            "pywin32-служба + install_agent.bat"
        )
        return 3

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
