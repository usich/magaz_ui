"""Тесты предполётной проверки сервера (`server.preflight`, Э7).

Смысл этих тестов — зафиксировать, что именно блокирует приёмку (`fail`), а что
только требует внимания (`warn`): на стенде от этого зависит, идём дальше или
сначала лечим окружение.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from sqlalchemy import Engine

from server import cli as server_cli
from server.config import Settings
from server.db.session import DatabaseManager
from server.preflight import (
    REQUIRED_MODULES,
    REQUIRED_TABLES,
    render_preflight,
    run_preflight,
)
from shared.preflight import CHECK_FAIL, CHECK_OK, CHECK_WARN

EXPECTED_CHECK_NAMES = (
    "Версия Python",
    "Зависимости сервера",
    "Схема БД",
    "Ревизия миграций",
    "Ключи шифрования",
    "Интерфейс (SPA)",
    "Настройки §4.9",
    "Администраторы",
    "Порт сервера",
    "Каталог логов",
    "Адрес для агентов",
    "Версия Magaz UI",
)


def _statuses(report) -> dict[str, str]:  # noqa: ANN001 - pytest-стиль без Protocol
    """Статусы проверок отчёта по имени пункта."""
    return {item.name: item.status for item in report.results}


class TestServerChecks:
    """Пункты проверки по отдельности — на изолированной временной БД."""

    def test_tables_ok_on_migrated_schema(
        self, engine: Engine, db_manager: DatabaseManager  # noqa: ARG002 - схема создаётся фикстурой
    ) -> None:
        from server.preflight import _check_tables

        result = _check_tables(db_manager)
        assert result.status == CHECK_OK
        assert str(len(REQUIRED_TABLES)) in result.detail

    def test_tables_fail_on_empty_database(self, settings: Settings, tmp_path: Path) -> None:
        from server.preflight import _check_tables

        empty = settings.model_copy(
            update={"database_url": f"sqlite:///{(tmp_path / 'empty.db').as_posix()}"}
        )
        manager = DatabaseManager(empty.database_url)
        try:
            result = _check_tables(manager)
        finally:
            manager.dispose()
        assert result.status == CHECK_FAIL
        assert "таблиц нет вообще" in result.detail
        assert "alembic upgrade head" in result.hint

    def test_admin_missing_is_failure(
        self, engine: Engine, db_manager: DatabaseManager  # noqa: ARG002 - нужна схема
    ) -> None:
        from server.preflight import _check_admin

        result = _check_admin(db_manager)
        assert result.status == CHECK_FAIL
        assert "create-admin" in result.hint

    def test_admin_ok(self, db_manager: DatabaseManager, admin) -> None:  # noqa: ANN001
        from server.preflight import _check_admin

        assert _check_admin(db_manager).status == CHECK_OK

    def test_settings_rows_ok_when_seeded(
        self, db_manager: DatabaseManager, default_settings_seeded: None
    ) -> None:
        from server.preflight import _check_settings_rows

        assert _check_settings_rows(db_manager).status == CHECK_OK

    def test_settings_rows_warn_without_seed(
        self, engine: Engine, db_manager: DatabaseManager  # noqa: ARG002 - нужна схема
    ) -> None:
        from server.preflight import _check_settings_rows

        result = _check_settings_rows(db_manager)
        assert result.status == CHECK_WARN
        assert "seed-settings" in result.hint

    def test_spa_missing_is_failure_while_serving(self, settings: Settings) -> None:
        from server.preflight import _check_spa

        result = _check_spa(settings)
        assert result.status == CHECK_FAIL
        assert "npm run build" in result.hint

    def test_spa_ok_when_built(self, settings: Settings) -> None:
        from server.preflight import _check_spa

        dist = Path(settings.frontend_dist_dir)
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text("<html></html>", encoding="utf-8")
        assert _check_spa(settings).status == CHECK_OK

    def test_spa_warns_when_serving_disabled(self, settings: Settings) -> None:
        from server.preflight import _check_spa

        assert _check_spa(settings.model_copy(update={"serve_spa": False})).status == CHECK_WARN

    def test_secrets_warn_when_files_absent(self, settings: Settings) -> None:
        from server.preflight import _check_secrets

        result = _check_secrets(settings)
        assert result.status == CHECK_WARN
        assert "secrets.key" in result.detail

    def test_secrets_ok_when_files_exist(self, settings: Settings) -> None:
        from server.preflight import _check_secrets

        settings.secrets_key_path.write_bytes(b"key")
        settings.cookie_secret_path.write_bytes(b"key")
        assert _check_secrets(settings).status == CHECK_OK

    def test_secrets_ok_when_given_by_env(self, settings: Settings, tmp_path: Path) -> None:
        from server.preflight import _check_secrets

        from_env = settings.model_copy(
            update={
                "secret_key": "from-env",
                "cookie_secret": "from-env",
                "secrets_key_path": tmp_path / "net-takogo.key",
            }
        )
        result = _check_secrets(from_env)
        assert result.status == CHECK_OK
        assert "переменными окружения" in result.detail

    def test_port_ok_when_free(self, settings: Settings) -> None:
        from server.preflight import _check_port

        free = settings.model_copy(update={"host": "127.0.0.1", "port": 0})
        assert _check_port(free).status == CHECK_OK

    def test_port_warns_when_busy(self, settings: Settings) -> None:
        from server.preflight import _check_port

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        try:
            port = sock.getsockname()[1]
            # На приёмке занятый порт — обычно признак, что сервер уже поднят
            result = _check_port(settings.model_copy(update={"host": "127.0.0.1", "port": port}))
        finally:
            sock.close()
        assert result.status == CHECK_WARN
        assert "занят" in result.detail

    def test_log_dir_failure_when_path_is_a_file(self, settings: Settings, tmp_path: Path) -> None:
        from server.preflight import _check_log_dir

        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        broken = settings.model_copy(update={"log_file": blocker / "server.log"})
        result = _check_log_dir(broken)
        assert result.status == CHECK_FAIL
        assert "права на запись" in result.hint

    def test_log_dir_ok_and_leaves_no_probe_file(self, settings: Settings) -> None:
        from server.preflight import _check_log_dir

        assert _check_log_dir(settings).status == CHECK_OK
        assert not list(settings.log_file.parent.glob(".preflight.tmp"))

    def test_public_url_ok_when_set(self, settings: Settings) -> None:
        from server.preflight import _check_public_url

        configured = settings.model_copy(update={"public_server_url": "http://10.0.0.1:8080"})
        assert _check_public_url(configured).status == CHECK_OK

    def test_public_url_warns_when_unset(self, settings: Settings) -> None:
        from server.preflight import _check_public_url

        result = _check_public_url(settings)
        assert result.status == CHECK_WARN
        assert "MAGAZ_PUBLIC_SERVER_URL" in result.detail

    def test_migrations_warn_without_alembic_version(
        self, engine: Engine, db_manager: DatabaseManager  # noqa: ARG002 - нужна схема
    ) -> None:
        from server.preflight import _check_migrations

        # Схему в тестах создаёт `Base.metadata.create_all`, отметки alembic нет
        result = _check_migrations(db_manager)
        assert result.status == CHECK_WARN
        assert "нет отметки alembic" in result.detail

    def test_modules_ok_in_this_environment(self) -> None:
        from server.preflight import _check_modules

        result = _check_modules()
        assert result.status == CHECK_OK
        assert str(len(REQUIRED_MODULES)) in result.detail

    def test_python_version_is_enough(self) -> None:
        from server.preflight import _check_python

        assert _check_python().status == CHECK_OK


class TestRunPreflight:
    """Полный отчёт: состав пунктов, итог и вывод для протокола."""

    def test_full_report_is_green_on_ready_stand(
        self,
        settings: Settings,
        engine: Engine,  # noqa: ARG002 - схема создаётся до отчёта
        db_manager: DatabaseManager,
        admin,  # noqa: ANN001 - фикстура conftest
        default_settings_seeded: None,
    ) -> None:
        dist = Path(settings.frontend_dist_dir)
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text("<html></html>", encoding="utf-8")
        settings.secrets_key_path.write_bytes(b"key")
        settings.cookie_secret_path.write_bytes(b"key")
        ready = settings.model_copy(
            update={"port": 0, "public_server_url": "http://10.0.0.1:8080"}
        )

        report = run_preflight(ready, db_manager)

        assert tuple(_statuses(report)) == EXPECTED_CHECK_NAMES
        assert report.failures == []
        assert report.ok is True
        assert report.exit_code == 0

    def test_report_marks_unready_stand(
        self,
        settings: Settings,
        engine: Engine,  # noqa: ARG002 - схема есть, но администратора нет
        db_manager: DatabaseManager,
    ) -> None:
        report = run_preflight(settings.model_copy(update={"port": 0}), db_manager)
        statuses = _statuses(report)
        assert statuses["Интерфейс (SPA)"] == CHECK_FAIL
        assert statuses["Администраторы"] == CHECK_FAIL
        assert statuses["Порт сервера"] == CHECK_OK
        assert report.ok is False
        assert report.exit_code == 1

    def test_json_rendering_for_protocol(
        self,
        settings: Settings,
        engine: Engine,  # noqa: ARG002 - схема нужна для отчёта
        db_manager: DatabaseManager,
    ) -> None:
        import json

        report = run_preflight(settings.model_copy(update={"port": 0}), db_manager)
        printed = render_preflight(report, as_json=True)
        # ASCII-экранирование: файл протокола должен читаться независимо от кодировки
        # перенаправленного `stdout` (на русском Windows это cp1251)
        assert printed.isascii()
        payload = json.loads(printed)
        assert payload["title"].startswith("Предполётная проверка сервера")
        assert payload["counts"]["fail"] >= 1
        assert len(payload["checks"]) == len(EXPECTED_CHECK_NAMES)
        assert payload["checks"][0]["check"] == EXPECTED_CHECK_NAMES[0]

    def test_text_rendering_lists_hints(
        self,
        settings: Settings,
        engine: Engine,  # noqa: ARG002 - схема нужна для отчёта
        db_manager: DatabaseManager,
    ) -> None:
        text = render_preflight(run_preflight(settings.model_copy(update={"port": 0}), db_manager))
        assert "-> cd frontend && npm ci && npm run build" in text
        assert "сначала устраните ошибки" in text


class TestServerCli:
    """CLI-пункт `preflight` (§14.1 п. 2): команда есть и принимает флаги."""

    def test_parser_accepts_preflight_json(self) -> None:
        args = server_cli._build_parser().parse_args(["preflight", "--json"])
        assert args.command == "preflight"
        assert args.json is True

    def test_parser_default_is_text_output(self) -> None:
        assert server_cli._build_parser().parse_args(["preflight"]).json is False

    @pytest.mark.parametrize("command", ["info", "preflight", "seed-settings"])
    def test_existing_commands_still_parse(self, command: str) -> None:
        assert server_cli._build_parser().parse_args([command]).command == command

    def test_main_runs_preflight_and_returns_report_code(
        self,
        settings: Settings,
        engine: Engine,  # noqa: ARG002 - схема нужна отчёту
        db_manager: DatabaseManager,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Проверка запускается из CLI — именно так её зовут на стенде.

        Тест появился после живого дефекта: команда была добавлена в парсер, но не
        подключена в `main` — CLI молча возвращал код 1 без единой строки отчёта.
        """
        import json

        monkeypatch.setattr("server.cli.get_settings", lambda: settings)
        monkeypatch.setattr("server.cli.DatabaseManager", lambda *_args, **_kwargs: db_manager)

        code = server_cli.main(["preflight", "--json"])

        printed = capsys.readouterr().out
        assert printed.isascii()  # JSON для протокола — без зависимости от cp1251
        payload = json.loads(printed)
        assert code == 1  # нет администратора и собранного интерфейса
        assert payload["ok"] is False
        assert len(payload["checks"]) == len(EXPECTED_CHECK_NAMES)

    def test_main_prints_text_report_by_default(
        self,
        settings: Settings,
        engine: Engine,  # noqa: ARG002 - схема нужна отчёту
        db_manager: DatabaseManager,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr("server.cli.get_settings", lambda: settings)
        monkeypatch.setattr("server.cli.DatabaseManager", lambda *_args, **_kwargs: db_manager)

        server_cli.main(["preflight"])

        printed = capsys.readouterr().out
        assert printed.startswith("== Предполётная проверка сервера")
        assert "ОШИБКА" in printed
        assert "-> cd frontend && npm ci && npm run build" in printed
