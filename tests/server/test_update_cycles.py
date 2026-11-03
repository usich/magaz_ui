"""Циклы обновления 1С (Д-29): границы прохода, «пачка запуска», фильтры журнала.

Проверяется то, из-за чего вкладка «Обновление 1С» врала:

* статусы этапов брались по последней задаче данного вида **за всю историю** —
  успешное слияние прошлого прохода светилось зелёным и после старта нового;
* колонка «Статус задачи» искала **любую** `failed`-задачу из 200 последних —
  старая ошибка висела красным даже после успешного повтора.

Границы цикла задаёт админ (`POST /update-cycles/new`); авто-закрытие по простою
по умолчанию выключено (`update_cycle_idle_min=0`) — проход может длиться дни.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from server.db.models import Command, Store, UpdateCycle
from server.services import settings_service, update_cycle_service
from shared.enums import CommandStatus
from shared.timeutil import utcnow

ACTIONS_URL = "/api/v1/admin/stores/{store_id}/actions"
BULK_URL = "/api/v1/admin/bulk/actions"
JOBS_URL = "/api/v1/admin/jobs"
CYCLES_URL = "/api/v1/admin/update-cycles"
BASE_URL = "/api/v1/admin/local-bases"


def add_store(session: Session, *, name: str, ip: str) -> Store:
    """Второй магазин: `ip_address` уникален среди активных (§4.1)."""
    session.rollback()
    store = Store(
        name=name,
        ip_address=ip,
        cluster_name="srv-1c",
        database_name="trade",
        version_1c="8.3.24.1548",
        user_name_1c="Администратор",
        agent_token=f"token-{ip}",
    )
    session.add(store)
    session.commit()
    return store


def stored_jobs(session: Session) -> list[Command]:
    """Все задачи из БД в порядке появления."""
    session.expire_all()
    return list(session.scalars(select(Command).order_by(Command.id)))


def current_cycle(auth_client: TestClient, scope: str = "stores") -> dict:
    """Ответ `GET /update-cycles/current` для области `scope`."""
    response = auth_client.get(f"{CYCLES_URL}/current", params={"scope": scope})
    assert response.status_code == 200, response.text
    return response.json()


def check_files(auth_client: TestClient, store: Store) -> dict:
    """Поставить «Проверку файлов» магазину (задача `check_files`)."""
    response = auth_client.post(
        ACTIONS_URL.format(store_id=store.id),
        json={"action": "check_files", "params": {"filename": "update"}},
    )
    assert response.status_code == 202, response.text
    return response.json()


class TestCycleLifecycle:
    """Открытие/переиспользование/закрытие цикла (Д-29)."""

    def test_requires_authentication(self, client: TestClient) -> None:
        assert client.get(f"{CYCLES_URL}/current").status_code == 401

    def test_current_is_null_before_first_operation(self, auth_client: TestClient) -> None:
        body = current_cycle(auth_client)
        assert body["scope"] == "stores"
        assert body["cycle"] is None, "цикл создаётся первой же операцией"
        assert body["suggest_new"] is False
        assert body["idle_min"] == 0, "Д-29: авто-закрытие по простою выключено"

    def test_operation_opens_cycle_and_marks_job(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        job = check_files(auth_client, store)["job"]
        assert job["cycle_id"] is not None
        assert job["batch_id"], "одиночный запуск тоже получает batch_id"

        cycle = current_cycle(auth_client)["cycle"]
        assert cycle["id"] == job["cycle_id"]
        assert cycle["is_open"] is True
        assert cycle["started_by"] == "admin"
        assert cycle["trigger_kind"] == "check_files"
        assert cycle["stats"]["total"] == 1

        stored = stored_jobs(session)
        assert stored[0].cycle_id == cycle["id"]
        assert stored[0].batch_id == job["batch_id"]

    def test_second_operation_reuses_same_cycle(
        self, auth_client: TestClient, store: Store
    ) -> None:
        first = check_files(auth_client, store)["job"]
        second = check_files(auth_client, store)["job"]

        assert first["cycle_id"] == second["cycle_id"], "проход ещё идёт — цикл тот же"
        assert first["batch_id"] != second["batch_id"], "пачка у каждого нажатия своя"
        assert current_cycle(auth_client)["cycle"]["stats"]["total"] == 2

    def test_new_cycle_clears_stage_statuses(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """Д-29: после «Начать новый цикл» статусы прошлого прохода не видны."""
        check_files(auth_client, store)
        old_id = current_cycle(auth_client)["cycle"]["id"]
        # Прошлый проход завершён (в проде задачи уже в `success`) — иначе они
        # были бы перенесены в новый цикл как активные
        done = stored_jobs(session)[0]
        done.status = CommandStatus.SUCCESS
        session.commit()

        response = auth_client.post(f"{CYCLES_URL}/new", json={"scope": "stores"})
        assert response.status_code == 201, response.text
        opened = response.json()
        new_id = opened["cycle"]["id"]
        assert new_id != old_id
        assert opened["transferred"] == 0
        assert opened["cycle"]["stats"]["total"] == 0, "в новом цикле задач ещё нет"
        assert "Начат новый цикл" in opened["message"]

        old = session.get(UpdateCycle, old_id)
        assert old is not None and old.closed_at is not None
        assert old.closed_reason == "manual"

        by_new = auth_client.get(JOBS_URL, params={"cycle_id": new_id}).json()
        assert by_new["items"] == [], "новый цикл пуст — статусы этапов «—»"
        by_old = auth_client.get(JOBS_URL, params={"cycle_id": old_id}).json()
        assert len(by_old["items"]) == 1, "история прошлого цикла доступна по фильтру"



    def test_new_cycle_transfers_active_jobs(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """Незавершённые задачи переносятся в новый цикл, чтобы не пропасть из UI."""
        job = check_files(auth_client, store)["job"]
        assert job["status"] == CommandStatus.QUEUED

        response = auth_client.post(
            f"{CYCLES_URL}/new", json={"scope": "stores", "transfer_active": True}
        )
        body = response.json()
        assert body["transferred"] == 1

        session.expire_all()
        moved = session.get(Command, job["id"])
        assert moved is not None
        assert moved.cycle_id == body["cycle"]["id"]
        assert body["cycle"]["stats"]["active"] == 1

    def test_new_cycle_can_leave_active_jobs_in_closed_cycle(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        job = check_files(auth_client, store)["job"]
        response = auth_client.post(
            f"{CYCLES_URL}/new", json={"scope": "stores", "transfer_active": False}
        )
        body = response.json()
        assert body["transferred"] == 0
        session.expire_all()
        kept = session.get(Command, job["id"])
        assert kept is not None and kept.cycle_id != body["cycle"]["id"]

    def test_close_cycle_endpoint(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        check_files(auth_client, store)
        cycle_id = current_cycle(auth_client)["cycle"]["id"]

        closed = auth_client.post(f"{CYCLES_URL}/{cycle_id}/close")
        assert closed.status_code == 200, closed.text
        assert closed.json()["active_left"] == 1, "задача ещё в очереди"
        assert current_cycle(auth_client)["cycle"] is None

        missing = auth_client.post(f"{CYCLES_URL}/9999/close")
        assert missing.status_code == 404

    def test_unknown_scope_is_422(self, auth_client: TestClient) -> None:
        response = auth_client.get(f"{CYCLES_URL}/current", params={"scope": "other"})
        assert response.status_code == 422



class TestBatch:
    """«Пачка запуска» (Д-29): один запуск — один `batch_id` и один цикл."""

    def test_bulk_shares_batch_and_cycle(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        add_store(session, name="Магазин №6", ip="10.10.5.11")

        response = auth_client.post(
            BULK_URL, json={"action": "check_files", "params": {"filename": "update"}, "all": True}
        )
        assert response.status_code == 202, response.text
        assert response.json()["total"] == 2

        stored = stored_jobs(session)
        assert len(stored) == 2
        assert len({job.batch_id for job in stored}) == 1, "одна пачка — один batch_id"
        assert len({job.cycle_id for job in stored}) == 1
        assert stored[0].batch_id is not None

        page = auth_client.get(JOBS_URL, params={"batch_id": stored[0].batch_id}).json()
        assert len(page["items"]) == 2, "фильтр по пачке возвращает ровно её задачи"

    def test_non_cycle_kind_does_not_open_cycle(
        self, auth_client: TestClient, store: Store
    ) -> None:
        """Сообщение пользователям — не операция обновления, цикл не открывает."""
        response = auth_client.post(
            ACTIONS_URL.format(store_id=store.id),
            json={"action": "send_message", "params": {"text_message": "Привет"}},
        )
        assert response.status_code == 202, response.text
        assert response.json()["job"]["cycle_id"] is None
        assert current_cycle(auth_client)["cycle"] is None


class TestLocalScope:
    """У локальных баз свой цикл (Д-29): «новый цикл» магазинов его не трогает."""

    def test_local_bulk_update_opens_local_cycle(self, auth_client: TestClient) -> None:
        base = auth_client.post(
            BASE_URL,
            json={
                "name": "Бухгалтерия",
                "version_1c": "8.3.24.1548",
                "cluster_name": "srv-1c",
                "database_name": "buh",
                "user_name_1c": "Администратор",
                "group": "Финансы",
            },
        ).json()
        response = auth_client.post(
            f"{BASE_URL}/bulk/update",
            json={
                "local_base_ids": [base["id"]],
                "kind": "check_files_local",
                "file_name": "update",
            },
        )
        assert response.status_code == 202, response.text

        local = current_cycle(auth_client, "local")
        assert local["cycle"] is not None
        assert local["cycle"]["stats"]["total"] == 1
        assert current_cycle(auth_client, "stores")["cycle"] is None, "циклы раздельные"

        opened = auth_client.post(f"{CYCLES_URL}/new", json={"scope": "local"}).json()
        # Активная задача перенесена в новый цикл (transfer_active по умолчанию)
        assert opened["transferred"] == 1
        assert opened["cycle"]["stats"]["active"] == 1
        assert current_cycle(auth_client, "local")["cycle"]["id"] == opened["cycle"]["id"]



class TestIdleAutoClose:
    """Необязательная страховка `update_cycle_idle_min` (Д-29, по умолчанию выкл.)."""

    def test_idle_closes_stale_cycle(self, session: Session) -> None:
        settings_service.set_value(session, update_cycle_service.IDLE_SETTING, 60)
        cycle = update_cycle_service.get_or_open(
            session, update_cycle_service.SCOPE_STORES, kind="check_files", actor="admin"
        )
        # «Простой»: цикл открыт два часа назад, задач в работе нет
        cycle.created_at = utcnow() - timedelta(hours=2)
        session.commit()

        fresh = update_cycle_service.get_or_open(
            session, update_cycle_service.SCOPE_STORES, kind="check_files", actor="admin"
        )

        assert fresh.id != cycle.id
        session.expire_all()
        stale = session.get(UpdateCycle, cycle.id)
        assert stale is not None
        assert stale.closed_reason == "idle"

    def test_zero_idle_never_closes(self, session: Session) -> None:
        """`0` — авто-закрытие выключено: цикл живёт, пока админ не нажмёт кнопку."""
        first = update_cycle_service.get_or_open(
            session, update_cycle_service.SCOPE_STORES, kind="check_files", actor="admin"
        )
        first.created_at = utcnow() - timedelta(days=3)
        session.commit()

        second = update_cycle_service.get_or_open(
            session, update_cycle_service.SCOPE_STORES, kind="check_files", actor="admin"
        )
        assert second.id == first.id, "долгий проход не разрезается по времени"

