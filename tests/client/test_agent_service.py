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

import re
import sys
from pathlib import Path
from types import SimpleNamespace

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
        self,
        command: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ветка «нет `pywin32`»: команда объясняет ситуацию и возвращает код.

        `service_available` подменяется на `False` сознательно. На сборочной машине
        `pywin32` **установлен** (requirements-build.txt), и настоящий вызов тронул бы
        живую службу: `install` зарегистрировал бы службу на exe с pytest, а `debug`
        запустил бы её на переднем плане и повесил прогон (находка Э7 — тесты гоняются
        на сборочной машине). Наличие `pywin32` проверяется отдельным тестом ниже.
        """
        # Две ссылки на функцию: CLI импортирует её к себе по имени (§7.5)
        monkeypatch.setattr(agent_cli, "service_available", lambda: False)
        monkeypatch.setattr(service, "service_available", lambda: False)

        code = agent_main([command])

        assert code == service.UNSUPPORTED_CODE
        output = capsys.readouterr().out
        assert command in output
        assert "--console" in output, "подсказка обязана предложить режим отладки"

    def test_command_argv_is_exe_and_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`HandleCommandLine` получает `[<exe>, <команда>]`, а не сырой `sys.argv`.

        Это единственный тест, заходящий в ветку «`pywin32` есть», поэтому сам модуль
        `win32serviceutil` подменяется (`sys.modules`): он импортируется внутри
        `run_service_command`, и реальный вызов на сборочной машине был бы опасен.
        """
        captured: list[list[str]] = []
        fake_module = SimpleNamespace(
            HandleCommandLine=lambda _cls, argv: captured.append(list(argv))
        )
        monkeypatch.setitem(sys.modules, "win32serviceutil", fake_module)
        monkeypatch.setattr(service, "service_available", lambda: True)
        monkeypatch.setattr(service, "build_service_class", lambda: "MagazAgentService")

        code = service.run_service_command("install", [r"C:\MagazAgent\magaz-agent.exe"])

        assert code == 0
        assert captured == [[r"C:\MagazAgent\magaz-agent.exe", "install"]]

    def test_run_service_command_without_pywin32_is_graceful(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service, "service_available", lambda: False)

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
        assert "PySide6.QtWidgets" in spec, "интерфейс трея — Qt (спека 1.22)"
        assert "pystray" in spec and "tkinter" in spec, (
            "прежние библиотеки должны быть исключены из сборки"
        )

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

    def test_specs_resolve_repo_root_from_deploy_dir(self) -> None:
        """`SPECPATH` — каталог спеки, значит корень репозитория — его родитель.

        Находка Э7: в обоих спеках стоял `parents[1]` — на уровень **выше** репозитория,
        и `deploy\\build_release.bat` падал с «script '...\\PycharmProjects\\client\\agent\\
        __main__.py' not found»: сборка релиза никогда не доходила до конца.
        """
        for name in ("agent.spec", "tray.spec"):
            spec = read_deploy(name)

            assert re.search(r"REPO_ROOT = Path\(SPECPATH\)\.resolve\(\)\.parent\b", spec), (
                f"{name}: корень репозитория считается от каталога спеки"
            )
            assert not re.search(r"REPO_ROOT\s*=.*parents\[1\]", spec), (
                f"{name}: `parents[1]` уводит выше репозитория"
            )

    def test_bat_files_use_crlf(self) -> None:
        """`cmd.exe` не переваривает `*.bat` с LF: скрипт рвётся на разборе блоков."""
        for path in sorted(DEPLOY_DIR.glob("*.bat")):
            raw = path.read_bytes()

            assert b"\r\n" in raw, f"{path.name}: нужны CRLF-переводы строк"
            assert b"\n" not in raw.replace(b"\r\n", b""), f"{path.name}: найден одиночный LF"

    def test_echo_and_rem_inside_blocks_have_no_parentheses(self) -> None:
        """Скобки в `echo`/`rem` **внутри блока** ломают разбор всего скрипта.

        Находка Э7: строка `echo [1/3] Сборка SPA (npm ci + npm run build) ...` внутри
        `if not defined SKIP_UI (...)` давала «... was unexpected at this time» — скрипт
        сборки падал до первого действия, поэтому `dist\\release\\` и не появлялся.
        Скобки в кавычках (пути вида `Program Files (x86)`) не считаются.
        """
        for path in sorted(DEPLOY_DIR.glob("*.bat")):
            depth = 0
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                lowered = stripped.lower()
                if lowered.startswith(("echo", "rem", "@echo")):
                    assert not (depth > 0 and "(" in stripped), (
                        f"{path.name}:{number}: скобка в {stripped.split()[0]} внутри блока — "
                        "используйте ярлык (goto) вместо вложенного блока"
                    )
                    continue
                code = re.sub(r'"[^"]*"', "", line)  # кавычки: пути со скобками
                depth += code.count("(") - code.count(")")
            assert depth == 0, f"{path.name}: непарные скобки"


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
