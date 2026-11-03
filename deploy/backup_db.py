"""Резервная копия БД сервера перед ремонтом или обновлением (§4, docs/RUNBOOK.md).

Копировать `magaz.db` «руками» при включённом WAL нельзя: часть данных лежит
в `magaz.db-wal`, и копия окажется неполной. Скрипт снимает согласованный
снимок через `sqlite3.Connection.backup()` (то же, что `.backup` в sqlite3
CLI): SQLite сам учитывает WAL, а запись не держит БД заблокированной надолго.

Запускать на самом сервере: SQLite в режиме WAL по сети (SMB) не
поддерживается — `database is locked`, `disk I/O error` и риск порчи файла.

    .venv\\Scripts\\python.exe deploy\\backup_db.py
    .venv\\Scripts\\python.exe deploy\\backup_db.py --suffix=-before-repair

Снимок кладётся рядом с БД: `magaz.db.bak-20261103-150512`. Сразу после
копирования выполняется `PRAGMA quick_check`. Для PostgreSQL скрипт ничего
не делает: копию снимает сама СУБД (`pg_dump`), а не дашборд.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import make_url

#: `python deploy\\backup_db.py` не добавляет корень репозитория в sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - зависит от способа запуска
    sys.path.insert(0, str(REPO_ROOT))

from server.config import get_settings  # noqa: E402 - путь к пакету задан выше


def sqlite_path(database_url: str) -> Path | None:
    """Путь к файлу SQLite из строки подключения; `None` — БД не SQLite."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        return None
    return Path(url.database.strip())


def backup(db_path: Path, suffix: str = "") -> Path:
    """Снять снимок БД рядом с файлом и проверить его `quick_check`."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = db_path.with_name(f"{db_path.name}.bak-{stamp}{suffix}")
    source = sqlite3.connect(str(db_path))
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    check = sqlite3.connect(str(target))
    try:
        verdict = check.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        check.close()
    if verdict != "ok":
        raise RuntimeError(f"снимок не прошёл quick_check: {verdict}")
    return target


def main(argv: list[str] | None = None) -> int:
    """Точка входа: `python deploy/backup_db.py [--suffix ДОБАВКА]`."""
    parser = argparse.ArgumentParser(description="Резервная копия БД сервера Magaz UI")
    parser.add_argument(
        "--suffix",
        default="",
        help="добавка к имени файла снимка, например before-repair",
    )
    args = parser.parse_args(argv)

    database_url = get_settings().database_url
    db_path = sqlite_path(database_url)
    if db_path is None:
        print(f"БД не SQLite ({database_url}): копию делает СУБД, например pg_dump")
        return 0
    if not db_path.exists():
        print(f"ОШИБКА: файла БД нет: {db_path}")
        return 1
    try:
        target = backup(db_path, args.suffix)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"ОШИБКА: бэкап не сделан: {exc}")
        return 1
    size = target.stat().st_size
    print(f"Бэкап БД: {target} ({size} байт), quick_check: ok")
    return 0


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    raise SystemExit(main())
