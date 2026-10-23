"""Проверка наличия бэкапа 1С (Д-21) — SMB-сервис.

Перенос алгоритма из исходного скрипта (pysmb): по пути бэкапа смотрим список
файлов и ищем в имени целевой даты (за сегодня или за вчера) в форматах
`%Y-%m-%d` и `%Y%m%d`. Путь может быть:

* сетевым UNC `\\\\server\\share\\...` — опрос через `SMBConnection` (как в скрипте);
* локальным `C:\\...` — список файлов через файловую систему Windows.

Проверка выполняется **с сервера** (агент не трогается). Для сетевого пути
нужны учётка: «Гость» (по умолчанию) или отдельный логин/пароль.

История не хранится — только факт «есть/нет бэкап» на текущий день.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from smb.SMBConnection import SMBConnection

logger = logging.getLogger(__name__)

__all__ = [
    "BackupCheckError",
    "BackupCheckResult",
    "apply_backup_settings",
    "check_backup",
    "parse_target_date",
    "resolve_unc_path",
]

#: Форматы даты, которые ищем в имени файла бэкапа (как в исходном скрипте)
DATE_FORMATS: tuple[str, ...] = ("%Y-%m-%d", "%Y%m%d")


class BackupCheckError(RuntimeError):
    """Не удалось проверить бэкап (путь/подключение/ошибка чтения)."""


@dataclass(slots=True)
class BackupCheckResult:
    """Итог проверки одного пути: найден ли бэкап за целевую дату."""

    found: bool
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.found


def parse_target_date(yesterday: bool, *, reference: date | None = None) -> str:
    """Целевая дата строкой `YYYY-MM-DD` (сегодня или вчера)."""
    base = reference or date.today()
    target = base - timedelta(days=1) if yesterday else base
    return target.isoformat()


def resolve_unc_path(path: str) -> tuple[str, str, str] | None:
    """Разобрать UNC-путь `\\\\host\\share\\подпуть` на (host, share, subpath).

    Возвращает `None`, если путь не сетевой (тогда проверяем локально).
    Share — первый сегмент после имени хоста; остальное — подпуть в шаре.
    """
    raw = (path or "").strip()
    if not raw.startswith("\\\\"):
        return None
    parts = [p for p in raw.split("\\") if p]
    # parts = [host, share, ...subpath]
    if len(parts) < 2:
        raise BackupCheckError(f"Неполный сетевой путь: {path}")
    host = parts[0]
    share = parts[1]
    subpath = "\\".join(parts[2:])
    return host, share, subpath


def _filename_has_date(filename: str, target_iso: str) -> bool:
    """Есть ли целевая дата в имени файла (в любом из форматов скрипта)."""
    name = filename or ""
    if target_iso in name:
        return True
    # Формат без разделителей `YYYYMMDD`
    compact = datetime.strptime(target_iso, "%Y-%m-%d").strftime("%Y%m%d")
    return compact in name


def _check_local(path: str, target_iso: str) -> BackupCheckResult:
    """Локальный путь: список файлов через файловую систему Windows."""
    import os

    try:
        entries = os.listdir(path)
    except OSError as exc:
        raise BackupCheckError(f"Не удалось прочитать папку {path}: {exc}") from exc

    for entry in entries:
        if _filename_has_date(entry, target_iso):
            return BackupCheckResult(found=True)
    return BackupCheckResult(found=False)


def _check_smb(
    unc: tuple[str, str, str],
    target_iso: str,
    *,
    client_machine: str,
    user: str,
    password: str,
) -> BackupCheckResult:
    """Сетевой путь через `SMBConnection.listPath` (алгоритм исходного скрипта).

    Пробует оба порта: сначала **445** (SMB без NetBIOS — так ходит проводник),
    затем **139** (NetBIOS Session Service). На многих машинах NetBIOS выключен,
    и 139 отвечает TCP-подключением, но сессия не стартует (`NotConnectedError`),
    поэтому без fallback доступа нет даже при открытых портах.
    """
    host, share, subpath = unc
    errors: list[str] = []
    for port in (445, 139):
        conn = SMBConnection(
            user or "Гость",
            password or "",
            client_machine or "magaz-server",
            host,
            use_ntlm_v2=True,
        )
        try:
            conn.connect(host, port, timeout=10)
        except Exception as exc:  # noqa: BLE001 - pysmb бросает разные типы
            errors.append(f"порт {port}: {type(exc).__name__} ({exc})")
            continue

        try:
            entries = conn.listPath(share, f"/{subpath}")
        except Exception as exc:  # noqa: BLE001 - ошибка чтения шары
            conn.close()
            errors.append(f"порт {port}, чтение: {type(exc).__name__} ({exc})")
            continue
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - закрытие не должно маскировать ошибку
                pass

        for entry in entries:
            if _filename_has_date(entry.filename, target_iso):
                return BackupCheckResult(found=True)
        return BackupCheckResult(found=False)

    raise BackupCheckError(
        f"Нет доступа к {host}\\{share}. Пробованы порты 445 и 139. "
        + ("; ".join(errors) if errors else "соединение не установлено")
        + ". Проверьте, что шаре нужна авторизация и в настройках указаны логин/пароль SMB."
    )

def check_backup(
    path: str,
    *,
    yesterday: bool,
    client_machine: str | None = None,
    use_auth: bool = False,
    user: str | None = None,
    password: str | None = None,
    reference: date | None = None,
) -> BackupCheckResult:
    """Проверить наличие бэкапа по пути за целевую дату (сегодня/вчера).

    Сетевой путь → SMB, локальный → файловая система. При «гостевом» режиме
    (`use_auth=False`) логин/пароль игнорируются — шлётся «Гость».
    """
    if not (path or "").strip():
        raise BackupCheckError("Не указан путь к бэкапу")
    target_iso = parse_target_date(yesterday, reference=reference)

    unc = resolve_unc_path(path)
    if unc is not None:
        # Основной способ для сетевого пути — файловая система Windows по UNC
        # (`os.listdir`), как ходит проводник: Windows сама аутентифицируется
        # текущей сессией/сохранёнными учётными. pysmb на многих современных
        # SMB-шарах не может установить сеанс (ConnectionReset/NotConnected),
        # поэтому используется как fallback.
        try:
            return _check_local(path, target_iso)
        except BackupCheckError:
            cred_user = user if use_auth else None
            cred_password = password if use_auth else None
            return _check_smb(
                unc,
                target_iso,
                client_machine=client_machine or "",
                user=cred_user or "",
                password=cred_password or "",
            )
    return _check_local(path.strip(), target_iso)

def apply_backup_settings(
    obj,
    *,
    enabled: bool | None = None,
    path: str | None = None,
    host: str | None = None,
    use_auth: bool | None = None,
    user: str | None = None,
    password: str | None = None,
    check_yesterday: bool | None = None,
    clear_password: bool = False,
    secret_box: SecretBox | None = None,
):
    """Применить настройки проверки бэкапа к объекту (Store или LocalBase).

    Пароль SMB шифруется Fernet (§8.3), наружу уходит только признак
    `backup_password_set`. `clear_password=True` стирает пароль.
    """
    if enabled is not None:
        obj.backup_check_enabled = bool(enabled)
    if path is not None:
        obj.backup_path = (path or "").strip() or None
    if host is not None:
        obj.backup_host = (host or "").strip() or None
    if use_auth is not None:
        obj.backup_use_auth = bool(use_auth)
    if user is not None:
        obj.backup_user = (user or "").strip() or None
    if check_yesterday is not None:
        obj.backup_check_yesterday = bool(check_yesterday)

    if clear_password:
        obj.backup_password_enc = None
    elif password:
        if secret_box is None:
            raise BackupCheckError("Не передан шифровальщик для пароля SMB")
        obj.backup_password_enc = secret_box.encrypt(password)
    return obj

