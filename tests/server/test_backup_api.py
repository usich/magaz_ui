"""API статусов бэкапов (Д-21): `/admin/backup/status` и `/admin/backup/list`.

Проверяем сводку «есть N из M» отдельно по магазинам и локальным базам и
объединённый список с включённой проверкой.
"""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from server.db.models import LocalBase, Store

STATUS_URL = "/api/v1/admin/backup/status"
STORES_URL = "/api/v1/admin/backup/stores"
LOCAL_BASES_URL = "/api/v1/admin/backup/local-bases"
CHECK_URL = "/api/v1/admin/backup/check"


def add_store(session: Session, name: str, *, enabled: bool, status: str = "") -> Store:
    # Уникальный IP и токен, чтобы не упереться в уникальные индексы (§4.1)
    ip = f"10.10.1.{hash(name) % 250 + 1}"
    row = Store(
        name=name,
        ip_address=ip,
        cluster_name="srv-1c",
        database_name="trade",
        version_1c="8.3.24.1548",
        user_name_1c="Администратор",
        agent_token=f"agent-token-{hash(name)}",
        backup_check_enabled=enabled,
        backup_status=status,
    )
    session.add(row)
    session.flush()
    return row


def add_local_base(session: Session, name: str, *, enabled: bool, status: str = "") -> LocalBase:
    row = LocalBase(
        name=name,
        version_1c="8.3.24.1548",
        cluster_name="srv-1c",
        database_name="buh",
        user_name_1c="Администратор",
        group="Финансы",
        backup_check_enabled=enabled,
        backup_status=status,
    )
    session.add(row)
    session.flush()
    return row


def test_backup_status_counts(auth_client: TestClient, session: Session) -> None:
    today = date.today().isoformat()
    store_ok = add_store(session, "Магазин №1", enabled=True, status="ok")
    store_ok.backup_checked_date = today
    store_missing = add_store(session, "Магазин №2", enabled=True, status="missing")
    store_missing.backup_checked_date = today
    add_store(session, "Магазин №3", enabled=False)  # не настроен — не в сводке
    base_ok = add_local_base(session, "Бухгалтерия", enabled=True, status="ok")
    base_ok.backup_checked_date = today
    session.commit()

    response = auth_client.get(STATUS_URL)
    assert response.status_code == 200, response.text
    data = response.json()

    assert data["stores"]["total"] == 2
    assert data["stores"]["ok"] == 1
    assert data["stores"]["missing"] == 1
    assert data["local_bases"]["total"] == 1
    assert data["local_bases"]["ok"] == 1
    assert data["local_bases"]["missing"] == 0


def test_backup_lists_are_separate(auth_client: TestClient, session: Session) -> None:
    add_store(session, "Магазин №1", enabled=True, status="missing")
    add_store(session, "Магазин №2", enabled=False)
    add_local_base(session, "Бухгалтерия", enabled=True, status="ok")
    add_local_base(session, "Зарплата", enabled=True, status="missing")
    session.commit()

    stores = auth_client.get(STORES_URL).json()
    assert [item["name"] for item in stores] == ["Магазин №1"]

    bases = auth_client.get(LOCAL_BASES_URL).json()
    assert [item["name"] for item in bases] == ["Бухгалтерия", "Зарплата"]


def test_backup_check_manual_only_missing(auth_client: TestClient, session: Session) -> None:
    """Ручная проверка опрашивает только не найденные (не делает лишних запросов)."""
    import datetime

    ok = add_store(session, "Магазин №1", enabled=True, status="ok")
    ok.backup_checked_date = datetime.date.today().isoformat()  # уже найден сегодня
    add_store(session, "Магазин №2", enabled=True, status="missing")
    session.commit()

    result = auth_client.post(CHECK_URL)
    assert result.status_code == 200, result.text
    assert result.json() == {"checked": 1}


def test_backup_status_requires_auth(client: TestClient) -> None:
    response = client.get(STATUS_URL)
    assert response.status_code == 401

def test_job_cancel_marks_canceled(auth_client: TestClient, session: Session) -> None:
    """Д-22: `POST /jobs/{id}/cancel` переводит активную задачу в `canceled`."""
    store_row = add_store(session, "Магазин №1", enabled=True)
    from server.services import command_service
    from shared.enums import CommandStatus

    command = command_service.create_command(
        session,
        store_id=store_row.id,
        kind="check_files",
        params={"filename": "update"},
        created_by="admin",
    )
    session.commit()

    response = auth_client.post(f"/api/v1/admin/jobs/{command.id}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "canceled"

    session.expire_all()
    fresh = session.get(type(command), command.id)
    assert fresh.status == CommandStatus.CANCELED


def test_job_cancel_already_finished_conflict(auth_client: TestClient, session: Session) -> None:
    """Завершённую задачу прервать нельзя — `409`."""
    store_row = add_store(session, "Магазин №1", enabled=True)
    from server.services import command_service
    from shared.enums import CommandStatus

    command = command_service.create_command(
        session, store_id=store_row.id, kind="check_files", params={}, created_by="admin"
    )
    command_service.set_status(session, command, CommandStatus.SUCCESS)
    session.commit()

    response = auth_client.post(f"/api/v1/admin/jobs/{command.id}/cancel")
    assert response.status_code == 409

