"""Предполётная проверка сервера перед приёмкой (Э7, §12).

Запуск на машине стенда:

    python -m server.cli preflight            # отчёт в консоль
    python -m server.cli preflight --json     # тот же отчёт в JSON — приложить к протоколу

Проверка **ничего не меняет** (читает БД и файлы, пробует занять порт и записать
пробный файл в каталог логов), поэтому безопасна на рабочем сервере. Отчёт
заполняется в `docs/ACCEPTANCE.md` — до первой живой операции, чтобы на стенде
не тратить время на «а база-то мигрирована?».

Почему отдельный CLI-пункт, а не «просто запустить сервер»: приёмка начинается с
чистой кассы, и первое, что ломается в реальности, — не код, а среда: не выполнен
`alembic upgrade head`, не создан администратор, не собран `frontend\\dist`.
"""

from __future__ import annotations

import importlib
import json
import socket
import sys
from pathlib import Path

from sqlalchemy import select

from server.config import MIGRATIONS_DIR, Settings, get_settings
from server.db.models import Setting
from server.db.session import DatabaseManager
from server.services import admin_service, settings_service
from shared.preflight import (
    CHECK_FAIL,
    CHECK_OK,
    CHECK_WARN,
    CheckResult,
    PreflightReport,
    check,
    console_safe,
)
from shared.version import APP_VERSION

__all__ = [
    "MIN_PYTHON",
    "REQUIRED_MODULES",
    "REQUIRED_TABLES",
    "render_preflight",
    "run_preflight",
]

#: Минимальная версия Python (§14.1 п. 1)
MIN_PYTHON = (3, 12)

#: Таблицы схемы (§4.1, §8.1): нет любой — дашборд не заработает
REQUIRED_TABLES = frozenset(
    {
        "stores",
        "admin_users",
        "commands",
        "job_logs",
        "file_checks",
        "store_devices",
        "notifications",
        "notification_targets",
        "notification_receipts",
        "settings",
        "audit_log",
    }
)

#: Модули из `requirements.txt`, без которых сервер не поднимется
REQUIRED_MODULES = (
    "fastapi",
    "uvicorn",
    "sqlalchemy",
    "alembic",
    "cryptography",
    "itsdangerous",
    "httpx",
    "pydantic_settings",
)


def _check_python() -> CheckResult:
    """Версия интерпретатора (§14.1 п. 1)."""
    needed = ".".join(str(part) for part in MIN_PYTHON)
    actual = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info[:2] >= MIN_PYTHON:
        return check("Версия Python", CHECK_OK, f"{actual} (нужно >= {needed})")
    return check(
        "Версия Python",
        CHECK_FAIL,
        f"{actual}, нужно >= {needed}",
        "установите Python 3.12+ и создайте venv (§14.1 п. 1)",
    )


def _check_modules() -> CheckResult:
    """Наличие зависимостей сервера: `pip install -r requirements.txt`."""
    broken: list[str] = []
    for name in REQUIRED_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - показываем причину как есть
            broken.append(f"{name} ({exc})")
    if not broken:
        return check("Зависимости сервера", CHECK_OK, f"{len(REQUIRED_MODULES)} модулей на месте")
    return check(
        "Зависимости сервера",
        CHECK_FAIL,
        "не работают: " + ", ".join(broken),
        "pip install -r requirements.txt в активированном venv",
    )




def _check_migrations(db: DatabaseManager) -> CheckResult:
    """Отметка Alembic в БД: все миграции применены.

    Проверка необязательная: базу можно создать и через `Base.metadata.create_all`
    (так делают тесты), тогда отметки нет — это предупреждение, а не ошибка.
    """
    try:
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        if not Path(MIGRATIONS_DIR).joinpath("versions").is_dir():
            return check("Ревизия миграций", CHECK_WARN, f"нет каталога {MIGRATIONS_DIR}")
        config = Config()
        config.set_main_option("script_location", str(MIGRATIONS_DIR))
        head = ScriptDirectory.from_config(config).get_current_head()
        with db.engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
    except Exception as exc:  # noqa: BLE001 - alembic может быть не установлен
        return check(
            "Ревизия миграций",
            CHECK_WARN,
            f"не удалось определить: {exc}",
            "запускайте из корня репозитория после pip install -r requirements.txt",
        )
    if current is None:
        return check(
            "Ревизия миграций",
            CHECK_WARN,
            "в БД нет отметки alembic",
            "alembic upgrade head (иначе схема может отличаться от кода)",
        )
    if current != head:
        return check(
            "Ревизия миграций",
            CHECK_WARN,
            f"в БД {current}, ожидается {head}",
            "alembic upgrade head",
        )
    return check("Ревизия миграций", CHECK_OK, f"head: {current}")


def _check_secrets(settings: Settings) -> CheckResult:
    """Файлы ключей шифрования (§8.3): без них пароли 1С не расшифровать."""
    if settings.secret_key and settings.cookie_secret:
        return check("Ключи шифрования", CHECK_OK, "заданы переменными окружения")
    paths = [settings.secrets_key_path, settings.cookie_secret_path]
    missing = [path.name for path in paths if not path.exists()]
    if not missing:
        return check("Ключи шифрования", CHECK_OK, f"файлы на месте ({paths[0].parent})")
    return check(
        "Ключи шифрования",
        CHECK_WARN,
        "нет файлов: " + ", ".join(missing),
        "создадутся при первом старте сервера; на боевом сервере восстановите их из "
        "бэкапа, иначе пароли 1С не расшифруются (§8.3)",
    )


def _check_spa(settings: Settings) -> CheckResult:
    """Собранный интерфейс (§5.2.1): приёмка идёт через дашборд."""
    dist_dir = settings.frontend_dist_dir
    if (dist_dir / "index.html").exists():
        return check("Интерфейс (SPA)", CHECK_OK, str(dist_dir))
    if settings.serve_spa:
        return check(
            "Интерфейс (SPA)",
            CHECK_FAIL,
            f"нет {dist_dir / 'index.html'}",
            "cd frontend && npm ci && npm run build (§5.2.1)",
        )
    return check("Интерфейс (SPA)", CHECK_WARN, "отдача SPA выключена (MAGAZ_SERVE_SPA=false)")


def _check_settings_rows(db: DatabaseManager) -> CheckResult:
    """Настройки §4.9 в БД (иначе часть функций работает по умолчаниям кода)."""
    with db.session() as session:
        existing = set(session.execute(select(Setting.key)).scalars())
    expected = set(settings_service.DEFAULT_SETTINGS)
    missing = sorted(expected - existing)
    if not missing:
        return check("Настройки §4.9", CHECK_OK, f"ключей: {len(expected)}")
    preview = ", ".join(missing[:4]) + ("..." if len(missing) > 4 else "")
    return check(
        "Настройки §4.9",
        CHECK_WARN,
        f"нет {len(missing)} из {len(expected)}: {preview}",
        "подсеются при старте сервера или python -m server.cli seed-settings",
    )


def _check_admin(db: DatabaseManager) -> CheckResult:
    """Действующие администраторы: без них в дашборд не войти (§14.1 п. 2)."""
    with db.session() as session:
        active = admin_service.count_active(session)
    if active:
        return check("Администраторы", CHECK_OK, f"действующих: {active}")
    return check(
        "Администраторы",
        CHECK_FAIL,
        "нет ни одного действующего администратора",
        "python -m server.cli create-admin --username admin (§14.1 п. 2)",
    )


def _check_port(settings: Settings) -> CheckResult:
    """Свободен ли порт сервера (занятый порт на приёмке — это нормально)."""
    host = settings.host if settings.host not in ("", "0.0.0.0") else "127.0.0.1"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.bind((host, settings.port))
        free = True
    except OSError:
        free = False
    finally:
        sock.close()
    if free:
        return check("Порт сервера", CHECK_OK, f"{host}:{settings.port} свободен")
    return check(
        "Порт сервера",
        CHECK_WARN,
        f"{host}:{settings.port} занят",
        "если сервер уже запущен — это ожидаемо; иначе см. RUNBOOK «порт занят»",
    )


def _check_log_dir(settings: Settings) -> CheckResult:
    """Каталог логов: запись нужна, чтобы приложить логи к протоколу (§9.2)."""
    directory = settings.log_file.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".preflight.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return check(
            "Каталог логов",
            CHECK_FAIL,
            f"{directory}: {exc}",
            "дайте права на запись каталогу данных (§14.2)",
        )
    return check("Каталог логов", CHECK_OK, str(directory))




def run_preflight(
    settings: Settings | None = None,
    db: DatabaseManager | None = None,
) -> PreflightReport:
    """Собрать отчёт предполётной проверки.

    `settings` и `db` можно передать снаружи — так делают тесты и CLI (он уже
    открыл `DatabaseManager` и не должен открывать второй).
    """
    settings = settings or get_settings()
    report = PreflightReport(
        title=f"Предполётная проверка сервера Magaz UI {APP_VERSION}"
    )
    report.add(_check_python())
    report.add(_check_modules())

    own_db = db is None
    if db is None:
        db = DatabaseManager(settings.database_url, echo=False)
    try:
        report.add(_check_tables(db))
        report.add(_check_migrations(db))
        report.add(_check_secrets(settings))
        report.add(_check_spa(settings))
        report.add(_check_settings_rows(db))
        report.add(_check_admin(db))
    finally:
        if own_db:
            db.dispose()

    report.add(_check_port(settings))
    report.add(_check_log_dir(settings))
    report.add(_check_public_url(settings))
    report.add(check("Версия Magaz UI", CHECK_OK, APP_VERSION))
    return report


def _check_public_url(settings: Settings) -> CheckResult:
    """Адрес для конфига агента (§11.2): на стенде видно, что подставится."""
    if settings.public_server_url:
        return check("Адрес для агентов", CHECK_OK, settings.public_server_url)
    return check(
        "Адрес для агентов",
        CHECK_WARN,
        "MAGAZ_PUBLIC_SERVER_URL не задан",
        "адрес будет взят из запроса администратора; на стенде задайте явно (§11.2)",
    )


def _check_tables(db: DatabaseManager) -> CheckResult:
    """Схема БД: все таблицы §4.1/§8.1 созданы."""
    from sqlalchemy import inspect

    existing = set(inspect(db.engine).get_table_names())
    missing = sorted(REQUIRED_TABLES - existing)
    if not missing:
        return check("Схема БД", CHECK_OK, f"таблиц: {len(REQUIRED_TABLES)}")
    detail = "таблиц нет вообще" if not existing else f"нет: {', '.join(missing)}"
    return check(
        "Схема БД",
        CHECK_FAIL,
        f"{db.engine.url.database} — {detail}",
        "alembic upgrade head из корня репозитория (§4.1)",
    )


def render_preflight(report: PreflightReport, *, as_json: bool = False) -> str:
    """Вывод отчёта: обычный текст для консоли или JSON для протокола.

    Текстовый вывод проходит через `console_safe`: отчёт печатают и в `cmd` на
    кассе, где кодировка cp1251 (§14.2). JSON наоборот выводится **ASCII-экранированным**
    (`\\uXXXX`): при `preflight --json > file` перенаправленный `stdout` на русском
    Windows пишет cp1251, и такой файл не прочитает ни один потребитель, ждущий UTF-8.
    """
    if as_json:
        return json.dumps(report.as_dict(), indent=2)
    return console_safe(report.render())


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    raise SystemExit(run_preflight().exit_code)
