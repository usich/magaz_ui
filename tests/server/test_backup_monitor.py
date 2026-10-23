"""Backup Monitor (Д-21): правила «раз в день + до 5 попыток с интервалом 3 мин».

`should_check_now` проверяется напрямую; `apply_statuses` — на реальных моделях
Store/LocalBase с подменённым `backup_service.check_backup` (сеть не дёргаем).
"""

from __future__ import annotations

from datetime import date

import pytest

from server.core.backup_monitor import apply_statuses, in_backup_window, should_check_now
from server.services import backup_service
from server.services.backup_service import BackupCheckResult


def test_should_check_ok_already_done() -> None:
    need, reason = should_check_now(
        backup_status="ok",
        backup_checked_date="2026-10-22",
        backup_checked_at=None,
        backup_tries=1,
        target_date="2026-10-22",
        max_tries=5,
        retry_interval_sec=180,
    )
    assert need is False and reason == "ok"


def test_should_check_new_day_resets() -> None:
    need, reason = should_check_now(
        backup_status="ok",
        backup_checked_date="2026-10-21",  # вчера
        backup_checked_at=None,
        backup_tries=5,
        target_date="2026-10-22",
        max_tries=5,
        retry_interval_sec=180,
    )
    assert need is True and reason == ""


def test_should_check_exhausted_after_max_tries() -> None:
    need, reason = should_check_now(
        backup_status="missing",
        backup_checked_date="2026-10-22",
        backup_checked_at=None,
        backup_tries=5,
        target_date="2026-10-22",
        max_tries=5,
        retry_interval_sec=180,
    )
    assert need is False and reason == "exhausted"


def test_should_check_too_soon_between_retries() -> None:
    from datetime import datetime, timedelta, timezone

    need, reason = should_check_now(
        backup_status="missing",
        backup_checked_date="2026-10-22",
        backup_checked_at=datetime.now(timezone.utc) - timedelta(seconds=30),
        backup_tries=1,
        target_date="2026-10-22",
        max_tries=5,
        retry_interval_sec=180,
    )
    assert need is False and reason == "too_soon"


def test_apply_statuses_marks_ok_when_found(session, store) -> None:  # noqa: ANN001
    store.backup_check_enabled = True
    store.backup_path = r"\\srv\backup\buh"
    session.flush()

    backup_service.check_backup = lambda *a, **k: BackupCheckResult(found=True)  # type: ignore[assignment]

    checked = apply_statuses(
        session,
        [store],
        [],
        secret_box=None,  # type: ignore[arg-type]
        max_tries=5,
        retry_interval_sec=180,
        reference=date(2026, 10, 22),
    )
    assert checked == 1
    assert store.backup_status == "ok"
    assert store.backup_checked_date == "2026-10-22"
    assert store.backup_tries == 1


def test_apply_statuses_marks_missing_and_increments_tries(session, store) -> None:  # noqa: ANN001
    store.backup_check_enabled = True
    store.backup_path = r"\\srv\backup\buh"
    session.flush()

    backup_service.check_backup = lambda *a, **k: BackupCheckResult(found=False)  # type: ignore[assignment]

    apply_statuses(
        session,
        [store],
        [],
        secret_box=None,  # type: ignore[arg-type]
        max_tries=5,
        retry_interval_sec=180,
        reference=date(2026, 10, 22),
    )
    assert store.backup_status == "missing"
    assert store.backup_tries == 1


def test_apply_statuses_skips_disabled(session, store) -> None:  # noqa: ANN001
    store.backup_check_enabled = False
    session.flush()

    checked = apply_statuses(
        session,
        [store],
        [],
        secret_box=None,  # type: ignore[arg-type]
        max_tries=5,
        retry_interval_sec=180,
        reference=date(2026, 10, 22),
    )
    assert checked == 0
    assert store.backup_status == ""



def test_in_backup_window_inside() -> None:
    """В окне 10:00–15:00 автоматическая проверка разрешена (Д-27)."""
    assert in_backup_window(10, start=10, end=15) is True
    assert in_backup_window(14, start=10, end=15) is True


def test_in_backup_window_before_start() -> None:
    """До 10:00 автоматическая проверка не делается."""
    assert in_backup_window(9, start=10, end=15) is False
    assert in_backup_window(0, start=10, end=15) is False


def test_in_backup_window_at_and_after_end() -> None:
    """С 15:00 и позже автоматическая проверка не делается (только вручную)."""
    assert in_backup_window(15, start=10, end=15) is False
    assert in_backup_window(16, start=10, end=15) is False
    assert in_backup_window(23, start=10, end=15) is False
