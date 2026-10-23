"""Обновление локальных баз 1С (Д-24): API, журнал, LocalUpdateMonitor.

Проверяется новый контракт `POST /admin/local-bases/{id}/run-update`, что задача
`run_update_db_local` попадает в `/jobs` с `local_base_name`, что диспетчер не
трогает локальные задачи (`list_pending`/`list_pending_local`), и что цикл
обновления (`update_db_cycle`) и монитор корректно закрывают задачу по логу 1С.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from client.agent.one_c import OneCPaths
from server.core.local_updater import LocalUpdateMonitor, update_db_cycle
from server.db.base import Base
from server.db.models import Command, JobLog, LocalBase
from server.db.session import DatabaseManager, create_db_engine
from server.services import command_service
from shared.enums import CommandKind, CommandStatus

BASE_URL = "/api/v1/admin/local-bases"


def make_payload(**overrides) -> dict:
    payload = {
        "name": "Бухгалтерия",
        "version_1c": "8.3.24.1548",
        "cluster_name": "srv-1c",
        "database_name": "buh",
        "user_name_1c": "Администратор",
        "group": "Финансы",
    }
    payload.update(overrides)
    return payload


def add_local_base(session: Session, secret_box, **overrides) -> LocalBase:
    """Создать локальную базу напрямую в БД (для монитора без HTTP)."""
    payload = make_payload(**overrides)
    base = LocalBase(
        name=payload["name"],
        version_1c=payload["version_1c"],
        cluster_name=payload["cluster_name"],
        database_name=payload["database_name"],
        user_name_1c=payload["user_name_1c"],
        group=payload.get("group", "Финансы"),
        password_enc=secret_box.encrypt("пароль-1с") if payload.get("password") else None,
    )
    session.add(base)
    session.commit()
    return base


class _FakeProvider:
    """Провайдер, имитирующий 1С: пишет в лог обновления нужный текст и выходит."""

    def __init__(self, work_dir: Path, text: str) -> None:
        self.work_dir = Path(work_dir)
        self.text = text

    async def run_captured(self, args, *, log_path=None, timeout=None):  # noqa: ANN001
        # 1С пишет итог в лог `/Out` (log-update-db.txt) — имитируем это:
        target = self.work_dir / "log-update-db.txt"
        target.write_text(self.text, encoding="utf-8")
        return 0

    async def run_quiet(self, args) -> bool:  # noqa: ANN001
        return True


def build_update_params_for_cycle():
    from server.core.local_updater import build_update_params
    from server.db.models import LocalBase

    base = LocalBase(
        name="Бухгалтерия",
        version_1c="8.3.24.1548",
        cluster_name="srv-1c",
        database_name="buh",
        user_name_1c="Администратор",
        group="Финансы",
    )
    return build_update_params(base, {"file_name": "update", "is_fast_update": True}, password="p")


def test_run_update_creates_local_job(auth_client: TestClient) -> None:
    """`POST .../run-update` ставит задачу `run_update_db_local`, ответ 202."""
    base = auth_client.post(BASE_URL, json=make_payload()).json()
    response = auth_client.post(f"{BASE_URL}/{base['id']}/run-update", json={"file_name": "update"})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["job"]["kind"] == "run_update_db_local"
    assert body["job"]["status"] == "queued"
    assert body["job"]["local_base_id"] == base["id"]
    assert body["job"]["local_base_name"] == "Бухгалтерия"
    assert "Пароль" not in response.text


def test_run_update_conflict_when_active(auth_client: TestClient) -> None:
    """Повторный запуск, пока задача активна, — `409` (R12 для локальных баз)."""
    base = auth_client.post(BASE_URL, json=make_payload()).json()
    first = auth_client.post(f"{BASE_URL}/{base['id']}/run-update", json={"file_name": "update"})
    assert first.status_code == 202
    second = auth_client.post(f"{BASE_URL}/{base['id']}/run-update", json={"file_name": "update"})
    assert second.status_code == 409
    assert "уже обновляется" in second.text


def test_run_update_file_cfe_is_extension(auth_client: TestClient, session: Session) -> None:
    """`.cfe` в имени файла помечает операцию как обновление расширения."""
    base = auth_client.post(BASE_URL, json=make_payload()).json()
    response = auth_client.post(f"{BASE_URL}/{base['id']}/run-update", json={"file_name": "pkg.cfe"})
    assert response.status_code == 202
    command = session.scalars(select(Command).order_by(Command.id.desc()).limit(1)).one()
    assert command.params["is_extension"] is True


def test_local_job_appears_in_jobs_with_local_base_name(auth_client: TestClient) -> None:
    """`GET /jobs?local_base_id=` отдаёт локальную задачу с именем базы."""
    base = auth_client.post(BASE_URL, json=make_payload()).json()
    auth_client.post(f"{BASE_URL}/{base['id']}/run-update", json={"file_name": "update"})

    listed = auth_client.get("/api/v1/admin/jobs", params={"local_base_id": base["id"]}).json()
    assert len(listed["items"]) == 1
    item = listed["items"][0]
    assert item["kind"] == "run_update_db_local"
    assert item["local_base_id"] == base["id"]
    assert item["local_base_name"] == "Бухгалтерия"
    assert item["store_id"] is None


def test_list_pending_excludes_local_commands(auth_client: TestClient, session: Session) -> None:
    """Диспетчер (`list_pending`) не видит локальные задачи, `list_pending_local` — видит."""
    base = auth_client.post(BASE_URL, json=make_payload()).json()
    auth_client.post(f"{BASE_URL}/{base['id']}/run-update", json={"file_name": "update"})
    session.expire_all()

    assert command_service.list_pending(session) == []
    pending_local = command_service.list_pending_local(session)
    assert len(pending_local) == 1
    assert pending_local[0].kind == "run_update_db_local"


async def test_update_db_cycle_success(tmp_path) -> None:
    """Цикл обновления по логу успеха закрывает задачу `success`."""
    paths = OneCPaths(work_dir=tmp_path, base_1c_path=tmp_path / "1c")
    up = build_update_params_for_cycle()
    provider = _FakeProvider(tmp_path, "Обновление конфигурации успешно завершено")
    status, _message = await update_db_cycle(up, paths, provider=provider, log_update=paths.log_update_db)
    assert status == CommandStatus.SUCCESS


async def test_update_db_cycle_failed(tmp_path) -> None:
    """Маркер ошибки в логе закрывает задачу `failed`."""
    paths = OneCPaths(work_dir=tmp_path, base_1c_path=tmp_path / "1c")
    up = build_update_params_for_cycle()
    provider = _FakeProvider(tmp_path, "Ошибка: не удалось обновить")
    status, message = await update_db_cycle(up, paths, provider=provider, log_update=paths.log_update_db)
    assert status == CommandStatus.FAILED
    assert "не выполнено" in message


def test_monitor_poll_once_runs_command(tmp_path, secret_box) -> None:
    """LocalUpdateMonitor выполняет поставленную задачу и ставит `success`."""
    db_url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    engine = create_db_engine(db_url)
    Base.metadata.create_all(engine)
    manager = DatabaseManager(db_url)

    try:
        with manager.session() as session:
            base = add_local_base(session, secret_box)
            cmd = command_service.create_local_command(
                session,
                local_base_id=base.id,
                kind=CommandKind.RUN_UPDATE_DB_LOCAL,
                params={"file_name": "update", "is_fast_update": True},
                created_by="admin",
            )
            session.commit()
            cmd_id = cmd.id

        settings = SimpleNamespace(
            local_1c_path=str(tmp_path / "1c"),
            local_update_dir=tmp_path,
            local_updater_startup_delay_sec=0,
            local_updater_interval_sec=5,
        )
        provider = _FakeProvider(tmp_path, "Обновление конфигурации успешно завершено")
        monitor = LocalUpdateMonitor(manager, settings, secret_box=secret_box, provider=provider)

        processed = asyncio.run(monitor.poll_once())
        assert processed == 1

        with manager.session() as session:
            job = command_service.get_command(session, cmd_id)
            assert job is not None
            assert job.status == "success"
            logs = list(session.scalars(select(JobLog).where(JobLog.command_id == cmd_id)))
            assert any("успешно завершено" in entry.message for entry in logs)
    finally:
        engine.dispose()


def test_bulk_update_creates_jobs_for_selected(auth_client: TestClient) -> None:
    """`POST /local-bases/bulk/update` ставит задачу каждой выбранной базе (Д-25)."""
    first = auth_client.post(BASE_URL, json=make_payload(name="Бухгалтерия")).json()
    second = auth_client.post(
        BASE_URL, json=make_payload(name="Торговля", database_name="trade")
    ).json()
    response = auth_client.post(
        f"{BASE_URL}/bulk/update",
        json={"local_base_ids": [first["id"], second["id"]], "file_name": "update"},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["total"] == 2
    assert len(body["jobs"]) == 2
    assert all(job["job_id"] is not None and job["status"] == "queued" for job in body["jobs"])
    assert "2 из 2" in body["message"]


def test_bulk_update_skips_busy_base(auth_client: TestClient) -> None:
    """База, которая уже обновляется, пропускается — остальные не блокируются (Д-25)."""
    busy = auth_client.post(BASE_URL, json=make_payload()).json()
    auth_client.post(f"{BASE_URL}/{busy['id']}/run-update", json={"file_name": "update"})
    free = auth_client.post(
        BASE_URL, json=make_payload(name="Торговля", database_name="trade")
    ).json()

    response = auth_client.post(
        f"{BASE_URL}/bulk/update",
        json={"local_base_ids": [busy["id"], free["id"]], "file_name": "update"},
    )
    assert response.status_code == 202
    body = response.json()
    by_id = {job["local_base_id"]: job for job in body["jobs"]}
    assert by_id[busy["id"]]["status"] == "skipped"
    assert "уже обновляется" in by_id[busy["id"]]["detail"]
    assert by_id[free["id"]]["status"] == "queued"


def test_bulk_update_empty_selection_is_422(auth_client: TestClient) -> None:
    """Массовый запуск без выбора баз — `422` (как `bulk/actions` без магазинов)."""
    response = auth_client.post(f"{BASE_URL}/bulk/update", json={"local_base_ids": []})
    assert response.status_code == 422


def test_local_update_concurrency_default(tmp_path, secret_box) -> None:  # noqa: ARG002
    """Настройка `local_update_concurrency` по умолчанию — 5 (Д-25)."""
    from server.db.base import Base
    from server.db.session import DatabaseManager, create_db_engine
    from server.services import settings_service

    db_url = f"sqlite:///{(tmp_path / 'c.db').as_posix()}"
    engine = create_db_engine(db_url)
    Base.metadata.create_all(engine)
    manager = DatabaseManager(db_url)
    try:
        with manager.session() as session:
            settings_service.ensure_defaults(session)
            session.commit()
            assert settings_service.get_int(session, "local_update_concurrency") == 5
    finally:
        engine.dispose()


def test_monitor_poll_once_runs_multiple_concurrently(tmp_path, secret_box) -> None:
    """LocalUpdateMonitor обрабатывает до `concurrency` задач за один цикл (Д-25)."""
    from server.db.base import Base
    from server.db.session import DatabaseManager, create_db_engine

    db_url = f"sqlite:///{(tmp_path / 'm2.db').as_posix()}"
    engine = create_db_engine(db_url)
    Base.metadata.create_all(engine)
    manager = DatabaseManager(db_url)
    try:
        with manager.session() as session:
            b1 = add_local_base(session, secret_box, name="База-1")
            b2 = add_local_base(session, secret_box, name="База-2", database_name="trade2")
            c1 = command_service.create_local_command(
                session,
                local_base_id=b1.id,
                kind=CommandKind.RUN_UPDATE_DB_LOCAL,
                params={"file_name": "update", "is_fast_update": True},
                created_by="admin",
            )
            c2 = command_service.create_local_command(
                session,
                local_base_id=b2.id,
                kind=CommandKind.RUN_UPDATE_DB_LOCAL,
                params={"file_name": "update", "is_fast_update": True},
                created_by="admin",
            )
            session.commit()
            c1_id, c2_id = c1.id, c2.id

        settings = SimpleNamespace(
            local_1c_path=str(tmp_path / "1c"),
            local_update_dir=tmp_path,
            local_updater_startup_delay_sec=0,
            local_updater_interval_sec=5,
        )
        provider = _FakeProvider(tmp_path, "Обновление конфигурации успешно завершено")
        monitor = LocalUpdateMonitor(
            manager, settings, secret_box=secret_box, provider=provider, concurrency_override=2
        )

        processed = asyncio.run(monitor.poll_once())
        assert processed == 2

        with manager.session() as session:
            assert command_service.get_command(session, c1_id).status == "success"
            assert command_service.get_command(session, c2_id).status == "success"
    finally:
        engine.dispose()
