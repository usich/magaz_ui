"""SMB-сервис проверки бэкапов (Д-21): дата, разбор UNC, поиск даты в имени.

Чистые функции проверяются напрямую; `check_backup` для локального пути — через
временную папку (SMB-подключение не дёргаем, сеть не нужна).
"""

from __future__ import annotations

from datetime import date

import pytest

from server.services.backup_service import (
    BackupCheckError,
    _filename_has_date,
    _check_local,
    check_backup,
    parse_target_date,
    resolve_unc_path,
)


def test_parse_target_date_today_and_yesterday() -> None:
    reference = date(2026, 10, 22)
    assert parse_target_date(False, reference=reference) == "2026-10-22"
    assert parse_target_date(True, reference=reference) == "2026-10-21"


def test_resolve_unc_path() -> None:
    host, share, sub = resolve_unc_path("\\\\srv\\backup\\buh\\2026-10-22_01.1cd")
    assert (host, share) == ("srv", "backup")
    assert "2026-10-22" in sub

    assert resolve_unc_path("C:/backup") is None
    assert resolve_unc_path("C:\\backup\\buh") is None


def test_resolve_unc_path_incomplete_raises() -> None:
    with pytest.raises(BackupCheckError):
        resolve_unc_path("\\\\srv")


def test_filename_has_date_iso_and_compact() -> None:
    assert _filename_has_date("buh_2026-10-22.zip", "2026-10-22") is True
    assert _filename_has_date("20261022.zip", "2026-10-22") is True  # без разделителей
    assert _filename_has_date("buh_2026-10-21.zip", "2026-10-22") is False


def test_check_backup_local_found_and_missing(tmp_path) -> None:  # noqa: ANN001
    (tmp_path / "buh_2026-10-22.zip").write_text("x")
    found = check_backup(str(tmp_path), yesterday=False, reference=date(2026, 10, 22))
    assert found.found is True
    assert found.error is None

    missing = check_backup(str(tmp_path), yesterday=True, reference=date(2026, 10, 22))
    assert missing.found is False  # за вчера файла нет


def test_check_backup_empty_path_raises() -> None:
    with pytest.raises(BackupCheckError):
        check_backup("  ", yesterday=False)


def test_check_local_missing_dir_reports_error(tmp_path) -> None:  # noqa: ANN001
    # Недоступная папка → BackupCheckError (монитор трактует как «бэкапа нет»)
    with pytest.raises(BackupCheckError):
        _check_local(str(tmp_path / "нет-такой-папки"), "2026-10-22")


def test_check_backup_unreachable_smb_host_is_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Недоступный SMB-хост не должен висеть на os.listdir/pysmb.

    TCP-probe (`_host_reachable`) отсекает хост сразу: `check_backup` возвращает
    `found=False` с ошибкой «недоступен», не трогая локальную ФС по UNC и pysmb.
    """
    import server.services.backup_service as bs

    calls: list[str] = []
    monkeypatch.setattr(bs, "_host_reachable", lambda *a, **k: False)
    monkeypatch.setattr(bs, "_check_local", lambda *a, **k: calls.append("_check_local"))
    monkeypatch.setattr(bs, "_check_smb", lambda *a, **k: calls.append("_check_smb"))

    result = check_backup(
        "\\\\недоступный-хост\\backup\\buh", yesterday=False, reference=date(2026, 10, 22)
    )

    assert result.found is False
    assert "недоступен" in (result.error or "")
    # Ни os.listdir по UNC, ни pysmb не вызывались — проверка мгновенная
    assert calls == []
