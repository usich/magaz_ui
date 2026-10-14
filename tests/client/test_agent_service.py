"""Служба Windows и скрипты установки (Э6, §7.1, §14.2).

Проверки идут без `pywin32` и без реальной службы (её поведение проверяется на
кассе, Э7). Что важно здесь:

* имя службы совпадает со спекой и скриптами (`MagazAgent`) — иначе установка
  «сработает», а дашборд не увидит магазин;
* без `pywin32` CLI не падает, а объясняет, что делать (иначе на кассе нет
  диагностики, §7.5), и возвращает ненулевой код — скрипт установки остановится;
* скрипты и спеки ссылаются на те же имена файлов, что и код (контракт
  развёртывания: `magaz-agent.exe`, `MagazTrayAgent.exe`, задача `MagazTrayAgent`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from client.agent import service
from client.agent import __main__ as agent_cli
from client.agent.__main__ import main as agent_main

#: Корень репозитория — ищем в нём `deploy\` со скриптами и специями
REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_ROOT / "deploy"


def read_deploy(name: str) -> str:
    """Прочитать файл развёртывания (кодировка bat-скриптов — UTF-8)."""
    path = DEPLOY_DIR / name
    assert path.is_file(), f"нет файла развёртывания {name}"
    return path.read_text(encoding="utf-8")


class TestServiceIdentity:
    """Имена службы и подсказки — общий контракт агента, спеки и скриптов."""

    def test_service_names_match_spec(self) -> None:
        # §7.1: служба `MagazAgent` под LocalSystem; имя видно в services.msc
        assert service.SERVICE_NAME == "MagazAgent"
        assert service.SERVICE_DISPLAY_NAME.startswith("Magaz UI")
        assert "1С" in service.SERVICE_DESCRIPTION

    def test_service_availability_detects_pywin32(self) -> None:
        """Без `pywin32` команды службы недоступны — и это видно заранее."""
        assert isinstance(service.service_available(), bool)
        try:
            import win32serviceutil  # noqa: F401
        except ImportError:
            assert service.service_available() is False

    def test_hint_mentions_build_requirements(self) -> None:
        assert "requirements-build.txt" in service.PYWIN32_HINT
        assert "INSTALL.md" in service.PYWIN32_HINT


class TestServiceCli:
    """CLI агента: команды службы не падают, а объясняют ситуацию (§7.5)."""

    @pytest.mark.parametrize("command", ["install", "remove", "start", "stop", "debug"])
    def test_commands_report_unsupported_and_return_code(
        self, command: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = agent_main([command])

        if service.service_available():  # pragma: no cover - прогон со сборочной машины
            assert code == 0
            return
        assert code == service.UNSUPPORTED_CODE
        output = capsys.readouterr().out
        assert command in output
        assert "--console" in output, "подсказка обязана предложить режим отладки"

    def test_run_service_command_without_pywin32_is_graceful(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        if service.service_available():  # pragma: no cover - сборочная машина
            pytest.skip("pywin32 установлен: проверяется на кассе (Э7)")

        code = service.run_service_command("start")

        assert code == service.UNSUPPORTED_CODE
        assert service.PYWIN32_HINT in capsys.readouterr().out


class TestDeployContract:
    """Скрипты развёртывания обязаны ссылаться на реальные имена exe и службы."""

    def test_agent_spec_builds_service_exe(self) -> None:
        spec = read_deploy("agent.spec")

        assert 'name="magaz-agent"' in spec
        assert "client" in spec and "agent" in spec
        assert "win32serviceutil" in spec, "без питон-модуля службы exe не установить"
        assert "uvicorn.protocols.http.auto" in spec, "uvicorn подгружает модули по строкам"

    def test_tray_spec_builds_windowless_exe(self) -> None:
        spec = read_deploy("tray.spec")

        assert 'name="MagazTrayAgent"' in spec
        assert "console=False" in spec, "чёрное окно на экране кассира недопустимо"
        assert "pystray" in spec and "tkinter" in spec

    def test_install_script_registers_service_and_task(self) -> None:
        script = read_deploy("install_agent.bat")

        assert "magaz-agent.exe\" install" in script
        assert "/tn MagazTrayAgent" in script, "трей обязан запускаться при входе (R3)"
        assert "onlogon" in script
        assert "Users" in script and "Пользователи" in script, "AD-агностичность (R9)"
        assert "--no-tray" in script, "должен быть флаг для машин без трея"
        assert "agent-config.json" in script, "конфиг берётся в карточке магазина (§11.2)"

    def test_uninstall_script_stops_service_and_task(self) -> None:
        script = read_deploy("uninstall_agent.bat")

        assert "magaz-agent.exe\" stop" in script
        assert "magaz-agent.exe\" remove" in script
        assert "/delete /f /tn MagazTrayAgent" in script
        assert "--purge" in script, "данные удаляются только по явному требованию"

    def test_build_script_covers_spa_and_both_exe(self) -> None:
        script = read_deploy("build_release.bat")

        assert "npm run build" in script, "SPA собирается в frontend\\dist (§14.1 п. 5)"
        assert "deploy\\agent.spec" in script
        assert "deploy\\tray.spec" in script
        assert "--skip-ui" in script

    def test_server_runner_uses_venv_and_module_entry(self) -> None:
        script = read_deploy("run_server.bat")

        assert ".venv\\Scripts\\python.exe" in script
        assert "-m server.main" in script

    def test_install_note_lists_deliverables(self) -> None:
        note = read_deploy("README_INSTALL.md")

        for expected in ("magaz-agent.exe", "MagazTrayAgent.exe", "install_agent.bat", "agent-config.json"):
            assert expected in note, f"в памятке нет {expected}"
        assert "tray.log" in note, "в памятке должно быть, где искать логи трея"


class TestAgentCliOptions:
    """Общие ключи CLI агента работают и до, и после подкоманды (§7.1).

    Живой прогон Э7 показал дефект: `python -m client.agent run --config <файл>`
    падал с «unrecognized arguments: --config ...». Именно так записаны примеры в
    документации и инструкция для кассы, поэтому ключи должны приниматься в обоих
    положениях, а значения из верхнего уровня — не затираться подкомандой.
    """

    def test_options_after_subcommand(self) -> None:
        args = agent_cli._build_parser().parse_args(
            ["run", "--config", "C:/MagazAgent/agent-config.json", "--console", "--host", "127.0.0.1"]
        )

        assert args.command == "run"
        assert args.config == "C:/MagazAgent/agent-config.json"
        assert args.console is True
        assert args.host == "127.0.0.1"
        assert args.log_level == "INFO"

    def test_options_before_subcommand(self) -> None:
        args = agent_cli._build_parser().parse_args(["--config", "x.json", "info"])

        assert args.command == "info"
        assert args.config == "x.json"
        assert args.console is False
        assert args.host == "0.0.0.0"

    def test_subcommand_does_not_reset_earlier_values(self) -> None:
        """Иначе значение из верхнего уровня затиралось бы значением по умолчанию."""
        args = agent_cli._build_parser().parse_args(["--port", "9000", "run", "--host", "127.0.0.1"])

        assert args.port == 9000
        assert args.host == "127.0.0.1"

    def test_plain_command_keeps_defaults(self) -> None:
        args = agent_cli._build_parser().parse_args(["preflight", "--no-server"])

        assert args.command == "preflight"
        assert args.no_server is True
        assert args.config is None
        assert args.port is None

    def test_service_commands_accept_common_options(self) -> None:
        args = agent_cli._build_parser().parse_args(["install", "--config", "x.json"])

        assert args.command == "install"
        assert args.config == "x.json"
