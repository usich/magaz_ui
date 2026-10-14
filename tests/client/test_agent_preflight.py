"""Тесты предполётной проверки кассы (`client.agent.preflight`, Э7).

Проверка запускается на живой кассе перед приёмкой, поэтому фиксируем главное:
чего не хватает — это `fail` (идти дальше нельзя), а что «штатно недоступно» —
`warn` (например, `msg.exe` на Windows Home, R2).
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest

from client.agent.__main__ import main as agent_main
from client.agent.config import LOG_DIR_ENV, AgentConfig
from client.agent.preflight import (
    ONE_C_EXE,
    SERVER_HEALTH_PATH,
    run_preflight,
)
from shared.preflight import CHECK_FAIL, CHECK_OK, CHECK_WARN

#: Версия платформы, которую «видит» проверка на тестовой кассе
FAKE_1C_VERSION = "8.3.24.1548"


def _free_port() -> int:
    """Свободный TCP-порт: проверка порта не должна зависеть от машины."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    try:
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def make_config(tmp_path: Path, **overrides: object) -> AgentConfig:
    """Конфиг «исправной кассы»: всё, что проверяет preflight, уже на месте."""
    base = Path(str(overrides.pop("base_1c_path", tmp_path / "1cv8")))
    (base / FAKE_1C_VERSION / "bin").mkdir(parents=True, exist_ok=True)
    (base / FAKE_1C_VERSION / "bin" / ONE_C_EXE).write_text("", encoding="utf-8")
    data: dict = {
        "store_id": 5,
        "store_name": "Магазин №5",
        "server_url": "http://10.0.0.1:8080",
        "agent_token": "token-from-store-card",
        "port": _free_port(),
        "base_1c_path": str(base),
        "work_dir": str(tmp_path / "work"),
    }
    data.update(overrides)
    return AgentConfig.model_validate(data)


def write_config(tmp_path: Path, config: AgentConfig, name: str = "agent-config.json") -> Path:
    """Файл `agent-config.json` для CLI-проверок (§7.2)."""
    path = tmp_path / name
    path.write_text(json.dumps(config.model_dump(), ensure_ascii=False), encoding="utf-8")
    return path


def statuses(report) -> dict[str, str]:  # noqa: ANN001 - pytest-стиль без Protocol
    """Статусы пунктов отчёта по имени."""
    return {item.name: item.status for item in report.results}


@pytest.fixture()
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Каталог логов агента во временной папке, а не в `C:\\ProgramData` (I-2)."""
    target = tmp_path / "logs"
    monkeypatch.setenv(LOG_DIR_ENV, str(target))
    return target


class TestConfigFile:
    """Конфиг: без него проверять нечего, поэтому это единственный `fail` в начале."""

    def test_missing_file_is_failure(self, tmp_path: Path) -> None:
        report = run_preflight(config_path=tmp_path / "нет-такого.json", check_server=False)
        assert report.ok is False
        assert statuses(report)["Конфиг агента"] == CHECK_FAIL
        assert "Не найден конфиг агента" in report.results[0].detail
        assert len(report.results) == 1  # дальше проверять нечего

    def test_broken_json_is_failure(self, tmp_path: Path) -> None:
        path = tmp_path / "agent-config.json"
        path.write_text("{не json", encoding="utf-8")
        report = run_preflight(config_path=path, check_server=False)
        assert statuses(report)["Конфиг агента"] == CHECK_FAIL

    def test_incomplete_config_lists_empty_fields(
        self, tmp_path: Path, log_dir: Path  # noqa: ARG002 - фикстура окружения
    ) -> None:
        config = make_config(tmp_path, store_id=0, agent_token="")
        report = run_preflight(config, check_server=False)
        result = report.results[0]
        assert result.status == CHECK_FAIL
        assert "store_id" in result.detail and "agent_token" in result.detail
        assert "карточки магазина" in result.hint

    def test_ready_config_is_ok(self, tmp_path: Path, log_dir: Path) -> None:  # noqa: ARG002
        report = run_preflight(make_config(tmp_path), check_server=False)
        assert statuses(report)["Конфиг агента"] == CHECK_OK


class TestOneCPlatform:
    """Платформа 1С: каталог версий и наличие `1cv8.exe` (§7.1, I-14)."""

    def test_ok_when_version_exists(self, tmp_path: Path) -> None:
        from client.agent.preflight import _check_one_c

        result = _check_one_c(make_config(tmp_path))
        assert result.status == CHECK_OK
        assert FAKE_1C_VERSION in result.detail

    def test_failure_when_base_path_missing(self, tmp_path: Path) -> None:
        from client.agent.preflight import _check_one_c

        config = make_config(tmp_path).model_copy(
            update={"base_1c_path": str(tmp_path / "нет-такого-каталога")}
        )
        result = _check_one_c(config)
        assert result.status == CHECK_FAIL
        assert "base_1c_path" in result.hint

    def test_failure_when_no_executable(self, tmp_path: Path) -> None:
        from client.agent.preflight import _check_one_c

        empty = tmp_path / "1cv8-empty"
        (empty / FAKE_1C_VERSION / "bin").mkdir(parents=True)
        config = make_config(tmp_path).model_copy(update={"base_1c_path": str(empty)})
        result = _check_one_c(config)
        assert result.status == CHECK_FAIL
        assert ONE_C_EXE in result.detail


class TestMsgChannel:
    """`msg.exe` — дублирующий канал (R2): недоступен, но приёмку не блокирует."""

    def test_warns_when_msg_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from client.agent.preflight import _check_msg

        monkeypatch.setattr("client.agent.preflight.shutil.which", lambda _name: None)
        result = _check_msg()
        assert result.status == CHECK_WARN
        assert "TrayAgent" in result.hint

    def test_ok_when_msg_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from client.agent.preflight import _check_msg

        monkeypatch.setattr(
            "client.agent.preflight.shutil.which", lambda _name: r"C:\Windows\System32\msg.exe"
        )
        result = _check_msg()
        assert result.status == CHECK_OK
        assert "msg.exe" in result.detail


class TestPortsAndDirectories:
    """Порт агента и каталоги: занятый порт — `warn`, нет записи — `fail`."""

    def test_ok_when_port_free(self, tmp_path: Path, log_dir: Path) -> None:  # noqa: ARG002
        from client.agent.preflight import _check_ports

        assert _check_ports(make_config(tmp_path)).status == CHECK_OK

    def test_warns_when_port_busy(self, tmp_path: Path) -> None:
        from client.agent.preflight import _check_ports

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("0.0.0.0", 0))
        try:
            port = int(sock.getsockname()[1])
            result = _check_ports(make_config(tmp_path, port=port))
        finally:
            sock.close()
        assert result.status == CHECK_WARN
        assert "занят" in result.detail

    def test_directories_ok_when_creatable(self, tmp_path: Path, log_dir: Path) -> None:
        from client.agent.preflight import _check_directories

        result = _check_directories(make_config(tmp_path))
        assert result.status == CHECK_OK
        assert log_dir.exists()

    def test_directories_failure_when_work_dir_is_a_file(self, tmp_path: Path, log_dir: Path) -> None:  # noqa: ARG002
        from client.agent.preflight import _check_directories

        blocker = tmp_path / "file-instead-of-dir"
        blocker.write_text("x", encoding="utf-8")
        result = _check_directories(make_config(tmp_path, work_dir=str(blocker)))
        assert result.status == CHECK_FAIL
        assert "LocalSystem" in result.hint


class TestCredentials:
    """Учётные данные 1С (§7.2, П-1): значения в отчёт не попадают (I-7)."""

    def test_ok_when_login_and_password_delivered(self, tmp_path: Path) -> None:
        from client.agent.preflight import _check_credentials

        config = make_config(tmp_path, one_c_profile={
            "user_name": "Администратор",
            "password_enc": "cipher-text",
            "credentials_version": 2,
        })
        result = _check_credentials(config)
        assert result.status == CHECK_OK
        assert "Администратор" in result.detail and "2" in result.detail

    def test_warns_when_nothing_delivered(self, tmp_path: Path) -> None:
        from client.agent.preflight import _check_credentials

        result = _check_credentials(make_config(tmp_path))
        assert result.status == CHECK_WARN
        assert "POST /credentials" in result.hint

    def test_encrypted_password_is_not_printed(self, tmp_path: Path, log_dir: Path) -> None:  # noqa: ARG002
        import json

        secret = "SUPER-SECRET-PAYLOAD"
        config = make_config(tmp_path, one_c_profile={"user_name": "u", "password_enc": secret})
        report = run_preflight(config, check_server=False)
        assert secret not in report.render()
        assert secret not in json.dumps(report.as_dict(), ensure_ascii=False)


class TestServerReachability:
    """`GET /api/v1/health` с кассы: главная «сеть»-проверка перед приёмкой."""

    @staticmethod
    def _respond(monkeypatch: pytest.MonkeyPatch, status_code: int, payload: dict) -> list[str]:
        """Подменить `httpx.get` и вернуть список запрошенных URL."""
        called: list[str] = []

        def fake_get(url: str, **_kwargs: object) -> httpx.Response:
            called.append(url)
            return httpx.Response(
                status_code=status_code,
                json=payload,
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr("httpx.get", fake_get)
        return called

    def test_ok_when_server_answers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from client.agent.preflight import _check_server

        config = make_config(tmp_path)
        called = self._respond(monkeypatch, 200, {"status": "online", "app_version": "1.10"})
        result = _check_server(config)
        assert result.status == CHECK_OK
        assert "1.10" in result.detail
        assert called == [f"{config.server_url}{SERVER_HEALTH_PATH}"]

    def test_failure_when_server_unreachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from client.agent.preflight import _check_server

        def boom(*_args: object, **_kwargs: object) -> httpx.Response:
            raise httpx.ConnectError("туннель не поднят")

        monkeypatch.setattr("httpx.get", boom)
        result = _check_server(make_config(tmp_path))
        assert result.status == CHECK_FAIL
        assert "туннель не поднят" in result.detail

    def test_failure_on_error_status(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from client.agent.preflight import _check_server

        self._respond(monkeypatch, 503, {})
        result = _check_server(make_config(tmp_path))
        assert result.status == CHECK_FAIL
        assert "503" in result.detail


#: Порядок пунктов полного отчёта — он же структура протокола приёмки кассы
EXPECTED_CHECK_NAMES = (
    "Конфиг агента",
    "Сервер доступен",
    "Платформа 1С",
    "Дублирующий канал msg.exe",
    "Порт агента",
    "Рабочая папка и логи",
    "Учётные данные 1С",
    "Версия MagazAgent",
)


class TestFullReportAndCli:
    """Полный отчёт и CLI-пункт `preflight` — то, что прикладывают к протоколу."""

    def test_full_report_is_green_on_ready_cash_desk(
        self, tmp_path: Path, log_dir: Path, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        TestServerReachability._respond(
            monkeypatch, 200, {"status": "online", "app_version": "1.10"}
        )
        config = make_config(
            tmp_path, one_c_profile={"user_name": "Администратор", "password_enc": "enc"}
        )

        report = run_preflight(config)

        assert tuple(statuses(report)) == EXPECTED_CHECK_NAMES
        assert report.failures == []
        assert report.ok is True
        assert report.exit_code == 0

    def test_warnings_do_not_block_acceptance(
        self, tmp_path: Path, log_dir: Path  # noqa: ARG002 - фикстура окружения
    ) -> None:
        # msg.exe не найден, пароль 1С ещё не доставлен: приёмку это не блокирует
        report = run_preflight(make_config(tmp_path), check_server=False)
        assert report.warnings
        assert report.ok is True
        assert "внимание" in report.summary()

    def test_cli_prints_json_and_returns_zero(
        self,
        tmp_path: Path,
        log_dir: Path,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = write_config(tmp_path, make_config(tmp_path))
        TestServerReachability._respond(
            monkeypatch, 200, {"status": "online", "app_version": "1.10"}
        )

        assert agent_main(["--config", str(path), "preflight", "--json"]) == 0

        printed = capsys.readouterr().out
        assert '"ok": true' in printed
        assert "Версия MagazAgent" in printed

    def test_cli_can_skip_server_check(
        self, tmp_path: Path, log_dir: Path, capsys: pytest.CaptureFixture[str]  # noqa: ARG002
    ) -> None:
        path = write_config(tmp_path, make_config(tmp_path))

        assert agent_main(["--config", str(path), "preflight", "--no-server"]) == 0

        assert "Сервер доступен" not in capsys.readouterr().out

    def test_cli_returns_one_without_config(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = agent_main(
            ["--config", str(tmp_path / "нет-конфига.json"), "preflight", "--no-server"]
        )

        printed = capsys.readouterr().out
        assert code == 1
        assert "Конфиг агента" in printed
        assert "ОШИБКА" in printed

    def test_render_text_is_readable_for_protocol(
        self, tmp_path: Path, log_dir: Path  # noqa: ARG002
    ) -> None:
        text = run_preflight(make_config(tmp_path), check_server=False).render()
        assert text.startswith("== Предполётная проверка кассы MagazAgent")
        assert "можно начинать приёмку" in text
        assert "token-from-store-card" not in text  # токен в отчёт не попадает (I-7)
