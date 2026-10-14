"""CLI сервера (§14.1 п. 2: «создание первого администратора CLI-командой»).

Примеры:

    python -m server.cli create-admin --username admin          # пароль спросит
    python -m server.cli list-admins
    python -m server.cli reset-password --username admin
    python -m server.cli seed-settings
    python -m server.cli info
    python -m server.cli preflight            # предполётная проверка перед приёмкой (Э7)
    python -m server.cli preflight --json     # отчёт для протокола docs/ACCEPTANCE.md
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from sqlalchemy import select

from server.config import DATA_DIR, get_settings
from server.db.models import AdminUser
from server.db.session import DatabaseManager
from server.services import admin_service, settings_service
from shared.version import APP_VERSION

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m server.cli", description="Служебные команды Magaz UI")
    parser.add_argument("--version", action="version", version=f"Magaz UI {APP_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-admin", help="создать администратора дашборда")
    create.add_argument("--username", required=True)
    create.add_argument("--password", help="пароль (не указывайте в командной строке без необходимости)")

    list_cmd = sub.add_parser("list-admins", help="показать администраторов")
    list_cmd.set_defaults(noop=True)

    reset = sub.add_parser("reset-password", help="сменить пароль администратора")
    reset.add_argument("--username", required=True)
    reset.add_argument("--password")

    sub.add_parser("seed-settings", help="добавить настройки по умолчанию §4.9")
    sub.add_parser("info", help="показать настройки подключения и версию")

    preflight = sub.add_parser("preflight", help="проверить готовность сервера к приёмке (Э7)")
    preflight.add_argument("--json", action="store_true", help="отчёт в JSON — приложить к протоколу")
    return parser


def _read_password(args: argparse.Namespace) -> str:
    """Пароль из аргумента либо интерактивно (ввод не отображается)."""
    if args.password:
        return args.password
    first = getpass.getpass("Пароль: ")
    second = getpass.getpass("Повторите пароль: ")
    if first != second:
        raise SystemExit("Пароли не совпадают")
    return first


def _cmd_create_admin(db: DatabaseManager, args: argparse.Namespace) -> int:
    settings = get_settings()
    password = _read_password(args)
    with db.session() as session:
        try:
            admin = admin_service.create_admin(
                session,
                username=args.username,
                password=password,
                min_password_length=settings.admin_password_min_length,
            )
        except (admin_service.AdminExistsError, admin_service.WeakPasswordError, ValueError) as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 2
        session.commit()
        print(f"Администратор создан: id={admin.id}, логин={admin.username}")
    return 0


def _cmd_list_admins(db: DatabaseManager) -> int:
    with db.session() as session:
        admins = list(session.scalars(select(AdminUser).order_by(AdminUser.id)).all())
        if not admins:
            print("Администраторов нет. Создайте: python -m server.cli create-admin --username admin")
            return 0
        print(f"{'id':>4}  {'логин':<24} {'активен':<8} последний вход")
        for admin in admins:
            last = admin.last_login_at.strftime("%Y-%m-%d %H:%M:%S") if admin.last_login_at else "—"
            print(f"{admin.id:>4}  {admin.username:<24} {str(admin.is_active):<8} {last}")
    return 0


def _cmd_reset_password(db: DatabaseManager, args: argparse.Namespace) -> int:
    settings = get_settings()
    password = _read_password(args)
    if len(password) < settings.admin_password_min_length:
        print(f"Ошибка: пароль короче {settings.admin_password_min_length} символов", file=sys.stderr)
        return 2
    with db.session() as session:
        admin = admin_service.get_by_username(session, args.username)
        if admin is None:
            print(f"Ошибка: администратор {args.username!r} не найден", file=sys.stderr)
            return 2
        from server.core.security import hash_password

        admin.password_hash = hash_password(password)
        session.commit()
        print(f"Пароль администратора {admin.username!r} изменён")
    return 0


def _cmd_seed_settings(db: DatabaseManager) -> int:
    with db.session() as session:
        added = settings_service.ensure_defaults(session)
        session.commit()
        print(f"Добавлено настроек: {added} (всего ключей §4.9: {len(settings_service.DEFAULT_SETTINGS)})")
    return 0


def _cmd_preflight(db: DatabaseManager, args: argparse.Namespace) -> int:
    """Предполётная проверка сервера (Э7): код возврата — из отчёта, не «успех любой ценой».

    Проверка ничего не меняет, поэтому её можно запускать и на уже работающем сервере:
    она читает БД, файлы и пробует занять порт. Отчёт прикладывается к протоколу
    приёмки (`docs/ACCEPTANCE.md`, раздел 1).
    """
    from server.preflight import render_preflight, run_preflight

    report = run_preflight(get_settings(), db)
    print(render_preflight(report, as_json=args.json))
    return report.exit_code


def _cmd_info(db: DatabaseManager) -> int:
    from sqlalchemy import inspect

    settings = get_settings()
    tables = set(inspect(db.engine).get_table_names())
    print(f"Magaz UI {APP_VERSION}")
    print(f"БД:            {settings.database_url}")
    print(f"Каталог данных: {DATA_DIR}")
    print(f"frontend/dist: {settings.frontend_dist_dir}")
    print(f"Хост/порт:     {settings.host}:{settings.port}")
    print(f"Схема БД:      {'есть' if 'stores' in tables else 'нет (выполните: alembic upgrade head)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI."""
    args = _build_parser().parse_args(argv)
    settings = get_settings()
    settings.ensure_directories()
    db = DatabaseManager(settings.database_url, echo=settings.sql_echo)
    try:
        if args.command == "create-admin":
            return _cmd_create_admin(db, args)
        if args.command == "list-admins":
            return _cmd_list_admins(db)
        if args.command == "reset-password":
            return _cmd_reset_password(db, args)
        if args.command == "seed-settings":
            return _cmd_seed_settings(db)
        if args.command == "preflight":
            return _cmd_preflight(db, args)
        if args.command == "info":
            return _cmd_info(db)
    finally:
        db.dispose()
    return 1


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    raise SystemExit(main())
