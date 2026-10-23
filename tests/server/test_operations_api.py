"""API операций, задач и матрицы файлов (§5.2.2, §11.3, §11.4, §11.6).

Проверяется контракт админских точек Э4: одиночные и массовые действия, история
задач с журналом, матрица файлов и досылка результата от агента (FR-23).

Диспетчер в этих тестах выключен (см. `conftest.settings`), поэтому статус
`queued` не «уезжает» в фоне: тест проверяет именно то, что вернул API.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from server.db.models import AuditLog, Command, FileCheck, Store
from shared.enums import CommandKind, CommandStatus
from shared.timeutil import utcnow

ACTIONS_URL = "/api/v1/admin/stores/{store_id}/actions"
BULK_URL = "/api/v1/admin/bulk/actions"
JOBS_URL = "/api/v1/admin/jobs"
FILES_URL = "/api/v1/admin/files/matrix"


def actions_url(store_id: int) -> str:
    """Адрес одиночного действия для магазина."""
    return ACTIONS_URL.format(store_id=store_id)


def audit_actions(session: Session) -> list[str]:
    """Действия аудита в порядке появления (§4.10)."""
    session.expire_all()
    return list(session.scalars(select(AuditLog.action).order_by(AuditLog.id)))


def jobs(session: Session) -> list[Command]:
    """Все задачи из БД в порядке появления."""
    session.expire_all()
    return list(session.scalars(select(Command).order_by(Command.id)))


def add_store(
    session: Session,
    *,
    name: str,
    ip: str,
    cluster: str,
    database: str = "trade",
    active: bool = True,
    extension_name: str | None = None,
) -> Store:
    """Второй магазин для массовых операций (§11.4).

    `database` нужен тестам запрета R12: «одна серверная ИБ» — это совпадение
    `cluster_name` + `database_name` (§9.2 FR-28).
    """
    session.rollback()
    store = Store(
        name=name,
        ip_address=ip,
        cluster_name=cluster,
        database_name=database,
        version_1c="8.3.24.1548",
        user_name_1c="Администратор",
        agent_token=f"token-{ip.replace('.', '-')}",
        is_active=active,
        extension_name=extension_name,
    )
    session.add(store)
    session.commit()
    return store


class TestAuthentication:
    """Правило «новый эндпоинт по умолчанию защищён» (§5.2.2, §8.1)."""

    @pytest.mark.parametrize(
        ("method", "url"),
        [
            ("post", actions_url(1)),
            ("post", BULK_URL),
            ("get", JOBS_URL),
            ("get", f"{JOBS_URL}/1"),
            ("get", FILES_URL),
            ("delete", f"{JOBS_URL}/1"),
            ("post", f"{JOBS_URL}/bulk-delete"),
        ],
    )
    def test_endpoints_require_session(
        self, client: TestClient, method: str, url: str
    ) -> None:
        if method == "get":
            response = client.get(url)
        elif method == "delete":
            response = client.delete(url)
        else:
            response = client.post(url, json={})
        assert response.status_code == 401


class TestSingleAction:
    """`POST /stores/{id}/actions` — постановка операции в очередь (§5.2.2)."""

    def test_check_files_action_creates_queued_job(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        response = auth_client.post(
            actions_url(store.id), json={"action": "check_files", "params": {"filename": "update"}}
        )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["job"]["status"] == CommandStatus.QUEUED
        assert body["job"]["store_id"] == store.id
        assert body["job"]["kind"] == CommandKind.CHECK_FILES
        assert body["warning"] is None
        assert "очередь" in body["message"]

        stored = jobs(session)
        assert len(stored) == 1
        assert stored[0].params == {"filename": "update"}
        assert stored[0].created_by == "admin"
        assert "store_action" in audit_actions(session)

    def test_merge_action_warns_when_files_were_never_checked(
        self, auth_client: TestClient, store: Store
    ) -> None:
        """Q53: отсутствие данных проверки не блокирует запуск, но предупреждает."""
        response = auth_client.post(
            actions_url(store.id), json={"action": "run_merge", "params": {"file_name": "update"}}
        )

        assert response.status_code == 202
        assert "не проверялись" in (response.json()["warning"] or "")

    def test_merge_extension_is_checked_against_cfe(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        session.add(
            FileCheck(
                store_id=store.id,
                filename="update",
                cf_exists=False,
                cfe_exists=True,
                xml_exists=True,
                checked_at=utcnow(),
            )
        )
        session.commit()

        response = auth_client.post(
            actions_url(store.id),
            json={
                "action": "run_merge",
                "params": {"file_name": "update", "extension_name": "МоёРасширение"},
            },
        )

        assert response.status_code == 202
        assert response.json()["warning"] is None, "для расширения нужен .cfe, а он есть"

    def test_merge_extension_from_store_card_is_checked_against_cfe(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """FR-4: имя расширения из карточки магазина (без `extension_name` в params).

        При массовой операции админ не повторяет имя в форме — оно берётся из
        карточки магазина. Предупреждение обязано проверять `.cfe`, а не `.cf`.
        """
        store.extension_name = "БГ_Торговоя"
        session.add(
            FileCheck(
                store_id=store.id,
                filename="update",
                cf_exists=False,
                cfe_exists=True,
                xml_exists=True,
                checked_at=utcnow(),
            )
        )
        session.commit()

        response = auth_client.post(
            actions_url(store.id),
            json={"action": "run_merge", "params": {"file_name": "update"}},
        )

        assert response.status_code == 202
        assert response.json()["warning"] is None, "имя из карточки: нужен .cfe, а он есть"

    def test_merge_extension_from_store_card_warns_about_cfe(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """FR-4: когда `.cfe` нет, предупреждение говорит про `.cfe` (имя из карточки)."""
        store.extension_name = "БГ_Торговоя"
        session.add(
            FileCheck(
                store_id=store.id,
                filename="update",
                cf_exists=True,
                cfe_exists=False,
                xml_exists=False,
                checked_at=utcnow(),
            )
        )
        session.commit()

        response = auth_client.post(
            actions_url(store.id),
            json={"action": "run_merge", "params": {"file_name": "update"}},
        )

        assert response.status_code == 202
        assert "update.cfe" in (response.json()["warning"] or "")

    def test_merge_extension_without_cfe_warns(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        session.add(
            FileCheck(
                store_id=store.id,
                filename="update",
                cf_exists=True,
                cfe_exists=False,
                xml_exists=False,
                checked_at=utcnow(),
            )
        )
        session.commit()

        response = auth_client.post(
            actions_url(store.id),
            json={
                "action": "run_merge",
                "params": {"file_name": "update", "extension_name": "МоёРасширение"},
            },
        )

        assert response.status_code == 202
        assert "update.cfe" in (response.json()["warning"] or "")

    def test_merge_cf_file_ignores_store_extension_warning(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """Д-16: явный `.cf` в имени файла → предупреждение про `.cf`, не `.cfe`.

        Даже когда в карточке магазина прописано расширение, имя `1cv8.cf`
        означает слияние основной конфигурации: проверяется наличие `.cf`.
        """
        store.extension_name = "БГ_Торговоя"
        session.add(
            FileCheck(
                store_id=store.id,
                filename="1cv8",
                cf_exists=False,
                cfe_exists=True,
                xml_exists=False,
                checked_at=utcnow(),
            )
        )
        session.commit()

        response = auth_client.post(
            actions_url(store.id),
            json={"action": "run_merge", "params": {"file_name": "1cv8.cf"}},
        )

        assert response.status_code == 202
        assert "1cv8.cf" in (response.json()["warning"] or "")

    def test_merge_action_is_quiet_when_file_exists(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        session.add(
            FileCheck(
                store_id=store.id,
                filename="update",
                cf_exists=True,
                cfe_exists=False,
                xml_exists=False,
                checked_at=utcnow(),
            )
        )
        session.commit()

        response = auth_client.post(
            actions_url(store.id), json={"action": "run_merge", "params": {"file_name": "update"}}
        )

        assert response.status_code == 202
        assert response.json()["warning"] is None

    def test_merge_action_keeps_is_extension(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """Д-17: явный `is_extension` для слияния сохраняется в `commands.params`."""
        response = auth_client.post(
            actions_url(store.id),
            json={
                "action": "run_merge",
                "params": {"file_name": "update", "is_extension": False},
            },
        )

        assert response.status_code == 202
        assert jobs(session)[0].params == {
            "file_name": "update",
            "full_merge": False,
            "is_extension": False,
        }

    def test_update_db_action_keeps_flag(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        response = auth_client.post(
            actions_url(store.id),
            json={"action": "run_update_db", "params": {"is_fast_update": True}},
        )

        assert response.status_code == 202
        assert jobs(session)[0].kind == CommandKind.RUN_UPDATE_DB
        assert jobs(session)[0].params == {"is_fast_update": True}

    def test_update_db_action_keeps_is_extension(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """Д-17: явный `is_extension` из формы сохраняется в `commands.params`."""
        response = auth_client.post(
            actions_url(store.id),
            json={
                "action": "run_update_db",
                "params": {"is_fast_update": True, "is_extension": False},
            },
        )

        assert response.status_code == 202
        assert jobs(session)[0].params == {"is_fast_update": True, "is_extension": False}

    def test_password_from_request_is_dropped(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """I-7/П-1: пароль не принимается из UI и не попадает в `commands.params`."""
        response = auth_client.post(
            actions_url(store.id),
            json={
                "action": "run_merge",
                "params": {"file_name": "update", "password": "секрет-1С"},
            },
        )

        assert response.status_code == 202
        assert "password" not in jobs(session)[0].params
        details = session.scalars(select(AuditLog.details)).all()
        assert "секрет-1С" not in str(details)
        assert "секрет-1С" not in response.text

    @pytest.mark.parametrize(
        ("action", "params", "expected"),
        [
            ("run_merge", {}, "file_name"),
            ("run_merge", {"file_name": "../evil"}, "разделителей"),
            ("download_file", {}, "url-file"),
            ("send_message", {}, "Текст сообщения"),
            ("notify", {"title": "x"}, "не может быть пустым"),
            ("hack_the_planet", {}, "Неизвестное действие"),
        ],
    )
    def test_invalid_actions_are_rejected(
        self,
        auth_client: TestClient,
        session: Session,
        store: Store,
        action: str,
        params: dict,
        expected: str,
    ) -> None:
        response = auth_client.post(actions_url(store.id), json={"action": action, "params": params})

        assert response.status_code == 422, response.text
        assert expected in response.text
        assert jobs(session) == [], "задача не создаётся при ошибке валидации"

    def test_disabled_store_action_is_conflict(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        store.is_active = False
        session.commit()

        response = auth_client.post(
            actions_url(store.id), json={"action": "check_files", "params": {}}
        )

        assert response.status_code == 409
        assert "отключён" in response.text

    def test_unknown_store_is_not_found(self, auth_client: TestClient) -> None:
        response = auth_client.post(
            actions_url(4242), json={"action": "check_files", "params": {}}
        )

        assert response.status_code == 404


class TestBulkAction:
    """`POST /bulk/actions` — массовые операции (§11.4: выбор / кластер / все)."""

    def test_bulk_by_cluster_targets_only_that_cluster(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        add_store(session, name="Магазин №6", ip="10.10.5.11", cluster="srv-1c")
        add_store(session, name="Магазин №7", ip="10.10.5.12", cluster="srv-2c")

        response = auth_client.post(
            BULK_URL,
            json={"action": "check_files", "params": {"filename": "update"}, "cluster": "srv-1c"},
        )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["total"] == 2, "магазин другого кластера не тронут"
        assert {item["store_name"] for item in body["jobs"]} == {"Магазин №5", "Магазин №6"}
        assert {stored.kind for stored in jobs(session)} == {CommandKind.CHECK_FILES}

    def test_bulk_by_store_ids_and_all(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        second = add_store(session, name="Магазин №6", ip="10.10.5.11", cluster="srv-1c")
        third = add_store(session, name="Магазин №7", ip="10.10.5.12", cluster="srv-2c")

        by_ids = auth_client.post(
            BULK_URL,
            json={"action": "send_message", "params": {"text_message": "Привет"}, "store_ids": [third.id]},
        )
        assert by_ids.status_code == 202
        assert by_ids.json()["total"] == 1
        assert by_ids.json()["jobs"][0]["store_id"] == third.id

        everyone = auth_client.post(
            BULK_URL, json={"action": "check_files", "params": {}, "all": True}
        )
        assert everyone.status_code == 202
        assert everyone.json()["total"] == 3
        assert second.id in {item["store_id"] for item in everyone.json()["jobs"]}

    def test_bulk_skips_disabled_stores(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        add_store(session, name="Магазин №6", ip="10.10.5.11", cluster="srv-1c", active=False)

        response = auth_client.post(BULK_URL, json={"action": "check_files", "params": {}, "all": True})

        assert response.status_code == 202
        statuses = {item["status"] for item in response.json()["jobs"]}
        assert "skipped" in statuses
        assert len(jobs(session)) == 1, "по отключённому магазину задача не создаётся"

    def test_bulk_download_file_is_fn2(self, auth_client: TestClient, session: Session, store: Store) -> None:
        """ФН2: рассылка ссылки на выбранные магазины (§11.3)."""
        response = auth_client.post(
            BULK_URL,
            json={
                "action": "download_file",
                "params": {"url-file": "https://disk.yandex.ru/d/XXXX/update.cf"},
                "store_ids": [store.id],
            },
        )

        assert response.status_code == 202
        stored = jobs(session)[0]
        assert stored.kind == CommandKind.DOWNLOAD_FILE
        assert stored.params == {"url-file": "https://disk.yandex.ru/d/XXXX/update.cf"}
        assert "bulk_action" in audit_actions(session)

    def test_bulk_without_selection_is_validation_error(self, auth_client: TestClient) -> None:
        response = auth_client.post(BULK_URL, json={"action": "check_files", "params": {}})

        assert response.status_code == 422
        assert "Не выбрано ни одного магазина" in response.text


class TestJobsApi:
    """`GET /jobs`, `GET /jobs/{id}` — история операций и полный журнал (§11.6)."""

    def test_jobs_list_has_filters_and_counters(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        auth_client.post(
            actions_url(store.id), json={"action": "check_files", "params": {"filename": "update"}}
        )
        auth_client.post(
            actions_url(store.id),
            json={"action": "run_update_db", "params": {"is_fast_update": True}},
        )

        response = auth_client.get(JOBS_URL)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 2
        assert body["active"] == 2, "обе задачи ещё не отправлены"
        assert body["counts"][CommandStatus.QUEUED] == 2
        assert [item["kind"] for item in body["items"]] == [
            CommandKind.RUN_UPDATE_DB,
            CommandKind.CHECK_FILES,
        ], "новые задачи — первыми"
        assert body["items"][0]["store_name"] == store.name
        assert body["items"][0]["last_message"], "последняя строка журнала видна в списке"

        filtered = auth_client.get(JOBS_URL, params={"kind": CommandKind.CHECK_FILES}).json()
        assert len(filtered["items"]) == 1
        assert filtered["items"][0]["kind"] == CommandKind.CHECK_FILES

        by_store = auth_client.get(JOBS_URL, params={"store_id": store.id}).json()
        assert len(by_store["items"]) == 2
        assert auth_client.get(JOBS_URL, params={"store_id": 999}).json()["items"] == []

    def test_job_detail_returns_full_journal(
        self, auth_client: TestClient, store: Store
    ) -> None:
        created = auth_client.post(
            actions_url(store.id), json={"action": "check_files", "params": {"filename": "update"}}
        ).json()
        job_id = created["job"]["id"]

        response = auth_client.get(f"{JOBS_URL}/{job_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == job_id
        assert body["params"] == {"filename": "update"}
        assert body["logs"], "журнал задачи не пуст"
        assert "admin" in body["logs"][0]["message"]
        assert body["logs"][0]["level"] == "info"

    def test_unknown_job_is_not_found(self, auth_client: TestClient) -> None:
        assert auth_client.get(f"{JOBS_URL}/4242").status_code == 404


class TestJobsDelete:
    """`DELETE /jobs/{id}` и `POST /jobs/bulk-delete` — удаление задач (§11.6, Э10)."""

    def test_delete_single_job_removes_it_with_journal(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        created = auth_client.post(
            actions_url(store.id), json={"action": "check_files", "params": {"filename": "update"}}
        ).json()
        job_id = created["job"]["id"]
        assert jobs(session), "до удаления задача есть"

        response = auth_client.delete(f"{JOBS_URL}/{job_id}")

        assert response.status_code == 200, response.text
        assert response.json()["deleted"] is True
        assert jobs(session) == [], "задача исчезла из БД"
        assert auth_client.get(f"{JOBS_URL}/{job_id}").status_code == 404
        assert "job_delete" in audit_actions(session), "действие зафиксировано в аудите"

    def test_delete_unknown_job_is_not_found(self, auth_client: TestClient) -> None:
        assert auth_client.delete(f"{JOBS_URL}/4242").status_code == 404

    def test_bulk_delete_removes_selected_and_skips_unknown(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        first = auth_client.post(
            actions_url(store.id), json={"action": "check_files", "params": {"filename": "update"}}
        ).json()["job"]["id"]
        second = auth_client.post(
            actions_url(store.id),
            json={"action": "run_update_db", "params": {"is_fast_update": True}},
        ).json()["job"]["id"]
        third = auth_client.post(
            actions_url(store.id), json={"action": "send_message", "params": {"text_message": "x"}}
        ).json()["job"]["id"]
        assert len(jobs(session)) == 3

        response = auth_client.post(
            f"{JOBS_URL}/bulk-delete", json={"ids": [first, third, 4242]}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["deleted"] == 2
        assert body["total"] == 3, "несуществующий id тоже учтён в запросе"
        remaining = [item.id for item in jobs(session)]
        assert remaining == [second], "удалены только выбранные"
        assert "job_bulk_delete" in audit_actions(session)

    def test_bulk_delete_empty_ids_is_422(self, auth_client: TestClient) -> None:
        response = auth_client.post(f"{JOBS_URL}/bulk-delete", json={"ids": []})
        assert response.status_code == 422


class TestFilesMatrix:
    """`GET /files/matrix` — матрица «магазин × (cf/cfe/xml)» (§11.3, Q52)."""

    def test_matrix_returns_last_snapshot_per_store(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        second = add_store(session, name="Магазин №6", ip="10.10.5.11", cluster="srv-1c")
        later = utcnow()
        earlier = later - timedelta(minutes=5)
        session.add_all(
            [
                FileCheck(
                    store_id=store.id,
                    filename="update",
                    cf_exists=False,
                    cfe_exists=False,
                    xml_exists=False,
                    checked_at=earlier,
                ),
                FileCheck(
                    store_id=store.id,
                    filename="update",
                    cf_exists=True,
                    cfe_exists=False,
                    xml_exists=True,
                    cf_size=2048,
                    xml_size=512,
                    checked_at=later,
                ),
            ]
        )
        session.commit()

        response = auth_client.get(FILES_URL, params={"filename": "update"})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 2
        assert body["present"] == 1, "файл есть у 1 из 2"
        assert body["missing_current"] == 1

        rows = {row["store_id"]: row for row in body["rows"]}
        assert rows[store.id]["cf_exists"] is True, "в матрице — последний снимок"
        assert rows[store.id]["cf_size"] == 2048
        assert rows[store.id]["checked"] is True
        assert rows[second.id]["checked"] is False
        assert rows[second.id]["has_file"] is False

    def test_matrix_uses_default_file_name(
        self, auth_client: TestClient, store: Store
    ) -> None:
        response = auth_client.get(FILES_URL)

        assert response.status_code == 200
        assert response.json()["filename"] == "update", "имя по умолчанию из §4.9"

    def test_matrix_rejects_path_separators(self, auth_client: TestClient) -> None:
        response = auth_client.get(FILES_URL, params={"filename": "../update"})

        assert response.status_code == 422

    def test_matrix_can_filter_by_cluster(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        add_store(session, name="Магазин №7", ip="10.10.5.12", cluster="srv-2c")

        response = auth_client.get(FILES_URL, params={"cluster": "srv-2c"})

        assert response.status_code == 200
        assert [row["store_name"] for row in response.json()["rows"]] == ["Магазин №7"]


class TestAgentCommandResult:
    """`POST /api/v1/agent/commands/{id}/result` — досылка результата (FR-23)."""

    def _create_job(self, auth_client: TestClient, store: Store) -> int:
        response = auth_client.post(
            actions_url(store.id), json={"action": "run_merge", "params": {"file_name": "update"}}
        )
        assert response.status_code == 202
        return int(response.json()["job"]["id"])

    def test_late_result_is_applied(
        self,
        auth_client: TestClient,
        session: Session,
        store: Store,
        agent_headers: dict[str, str],
    ) -> None:
        job_id = self._create_job(auth_client, store)

        response = auth_client.post(
            f"/api/v1/agent/commands/{job_id}/result",
            json={
                "store_id": store.id,
                "status": "success",
                "message": "Merge completed successfully.",
                "logs": "Объединение конфигураций успешно завершено",
            },
            headers=agent_headers,
        )

        assert response.status_code == 200, response.text
        assert response.json()["duplicate"] is False
        stored = session.get(Command, job_id)
        session.refresh(stored)
        assert stored.status == CommandStatus.SUCCESS
        assert "успешно завершено" in (stored.log_excerpt or "")

    def test_repeated_result_is_duplicate(
        self,
        auth_client: TestClient,
        store: Store,
        agent_headers: dict[str, str],
    ) -> None:
        job_id = self._create_job(auth_client, store)
        payload = {"store_id": store.id, "status": "failed", "message": "Ошибка"}

        first = auth_client.post(
            f"/api/v1/agent/commands/{job_id}/result", json=payload, headers=agent_headers
        )
        second = auth_client.post(
            f"/api/v1/agent/commands/{job_id}/result",
            json={"store_id": store.id, "status": "success"},
            headers=agent_headers,
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["duplicate"] is True
        assert second.json()["status"] == CommandStatus.FAILED, "итог не переписывается"

    def test_result_for_foreign_command_is_not_found(
        self,
        auth_client: TestClient,
        session: Session,
        store: Store,
        agent_headers: dict[str, str],
    ) -> None:
        """Агент магазина не может закрыть чужую задачу (§5.2.2)."""
        other = add_store(session, name="Магазин №9", ip="10.10.5.19", cluster="srv-3c")
        foreign_job = self._create_job(auth_client, other)

        response = auth_client.post(
            f"/api/v1/agent/commands/{foreign_job}/result",
            json={"store_id": store.id, "status": "success"},
            headers=agent_headers,
        )

        assert response.status_code == 404

    def test_invalid_status_is_rejected(
        self,
        auth_client: TestClient,
        store: Store,
        agent_headers: dict[str, str],
    ) -> None:
        job_id = self._create_job(auth_client, store)

        response = auth_client.post(
            f"/api/v1/agent/commands/{job_id}/result",
            json={"store_id": store.id, "status": "queued"},
            headers=agent_headers,
        )

        assert response.status_code == 422

    def test_agent_endpoint_requires_token_or_right_ip(
        self, auth_client: TestClient, store: Store
    ) -> None:
        job_id = self._create_job(auth_client, store)

        without_token = auth_client.post(
            f"/api/v1/agent/commands/{job_id}/result",
            json={"store_id": store.id, "status": "success"},
            headers={"X-Forwarded-For": store.ip_address},
        )
        from_foreign_ip = auth_client.post(
            f"/api/v1/agent/commands/{job_id}/result",
            json={"store_id": store.id, "status": "success"},
            headers={"X-Agent-Token": store.agent_token, "X-Forwarded-For": "10.99.99.99"},
        )

        assert without_token.status_code == 401
        assert from_foreign_ip.status_code == 403


class TestUpdateConflictBlock:
    """Жёсткий запрет одновременного обновления одной ИБ (R12, §9.2 FR-28, П-5).

    Проверяется то, что видит админ: запуск обновления отклоняется `409`, задачи
    не создаются, а текст объясняет, кого исключить. Действия, не меняющие ИБ,
    проходят. Диспетчер в тестах выключен, поэтому созданная задача остаётся
    «в работе» и играет роль «ИБ уже занята».
    """

    def _launch(self, client: TestClient, store: Store, action: str = "run_merge") -> int:
        """Поставить обновление магазину и вернуть id задачи (ожидается `202`)."""
        params = {"file_name": "update"} if action == "run_merge" else {"is_fast_update": False}
        response = client.post(actions_url(store.id), json={"action": action, "params": params})
        assert response.status_code == 202, response.text
        return int(response.json()["job"]["id"])

    @pytest.mark.parametrize("action", ["run_merge", "run_update_db"])
    def test_bulk_update_of_one_database_is_rejected(
        self, auth_client: TestClient, session: Session, store: Store, action: str
    ) -> None:
        """Два магазина одной ИБ в одном запуске — `409` и ни одной задачи (FR-28)."""
        second = add_store(session, name="Магазин №6", ip="10.10.5.11", cluster="srv-1c")
        params = {"file_name": "update"} if action == "run_merge" else {"is_fast_update": False}

        response = auth_client.post(
            BULK_URL, json={"action": action, "params": params, "all": True}
        )

        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert "R12" in detail
        assert "одной серверной ИБ" in detail
        assert store.name in detail and second.name in detail, "видно, кого исключить"
        assert jobs(session) == [], "при конфликте задачи не создаются вовсе"

    def test_launch_on_same_store_is_rejected(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """Повторный запуск обновления на том же магазине («одного ip») — `409`."""
        first = self._launch(auth_client, store)

        response = auth_client.post(
            actions_url(store.id),
            json={"action": "run_update_db", "params": {"is_fast_update": False}},
        )

        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert "уже выполняет операцию" in detail
        assert f"#{first}" in detail
        assert len(jobs(session)) == 1, "вторая задача не создана"

    def test_launch_on_another_store_of_same_database_is_rejected(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """ИБ занята другим магазином — `409` с указанием, кто её обновляет."""
        second = add_store(session, name="Магазин №6", ip="10.10.5.11", cluster="srv-1c")
        self._launch(auth_client, store)

        response = auth_client.post(
            actions_url(second.id), json={"action": "run_merge", "params": {"file_name": "update"}}
        )

        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert store.name in detail and "уже обновляет" in detail
        assert len(jobs(session)) == 1

    def test_launch_is_allowed_after_previous_job_finished(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """Пока задача не завершена — запрет; после завершения запуск проходит."""
        self._launch(auth_client, store)
        stored = jobs(session)[0]
        stored.status = CommandStatus.SUCCESS
        session.commit()

        response = auth_client.post(
            actions_url(store.id),
            json={"action": "run_update_db", "params": {"is_fast_update": True}},
        )

        assert response.status_code == 202, response.text
        assert len(jobs(session)) == 2

    def test_actions_that_do_not_change_database_are_allowed(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """`check_files`, `download_file`, `send_message` не блокируются (§11.4)."""
        second = add_store(session, name="Магазин №6", ip="10.10.5.11", cluster="srv-1c")
        self._launch(auth_client, store)

        check = auth_client.post(
            BULK_URL,
            json={"action": "check_files", "params": {"filename": "update"}, "all": True},
        )
        download = auth_client.post(
            BULK_URL,
            json={
                "action": "download_file",
                "params": {"url-file": "https://disk.yandex.ru/d/XXXX/update.cf"},
                "store_ids": [store.id, second.id],
            },
        )
        notice = auth_client.post(
            actions_url(store.id),
            json={
                "action": "send_message",
                "params": {"text_message": "База будет обновлена через 5 минут"},
            },
        )

        assert check.status_code == 202, check.text
        assert download.status_code == 202, download.text
        assert notice.status_code == 202, notice.text

    @pytest.mark.parametrize(
        ("cluster", "database"),
        [("srv-1c", "trade2"), ("srv-9c", "trade")],
    )
    def test_other_database_or_cluster_is_allowed(
        self,
        auth_client: TestClient,
        session: Session,
        store: Store,
        cluster: str,
        database: str,
    ) -> None:
        """Разные ИБ (`cluster_name` или `database_name`) не конфликтуют (FR-28)."""
        add_store(session, name="Магазин №6", ip="10.10.5.11", cluster=cluster, database=database)

        response = auth_client.post(
            BULK_URL,
            json={"action": "run_merge", "params": {"file_name": "update"}, "all": True},
        )

        assert response.status_code == 202, response.text
        assert response.json()["total"] == 2

    def test_store_without_database_does_not_block_others(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """Пустая база = ИБ не определена: запрет по паре не применяется (FR-28)."""
        add_store(session, name="Магазин №6", ip="10.10.5.11", cluster="srv-1c", database="")

        response = auth_client.post(
            BULK_URL,
            json={"action": "run_merge", "params": {"file_name": "update"}, "all": True},
        )

        assert response.status_code == 202, response.text
        assert len(jobs(session)) == 2

    def test_disabled_store_does_not_block_launch(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """Отключённый магазин пропускается и ИБ не занимает (§11.4, FR-28)."""
        add_store(session, name="Магазин №6", ip="10.10.5.11", cluster="srv-1c", active=False)

        response = auth_client.post(
            BULK_URL,
            json={"action": "run_merge", "params": {"file_name": "update"}, "all": True},
        )

        assert response.status_code == 202, response.text
        assert len(jobs(session)) == 1


class TestDeleteFileAction:
    """Э10: удаление файлов обновления на кассе — действие `delete_file` (§11.3)."""

    def test_action_creates_delete_file_job(  # noqa: ANN001
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        response = auth_client.post(
            actions_url(store.id),
            json={"action": "delete_file", "params": {"file_name": "update", "suffix": "cf"}},
        )

        assert response.status_code == 202, response.text
        assert response.json()["job"]["kind"] == CommandKind.DELETE_FILE
        stored = jobs(session)
        assert stored[0].kind == str(CommandKind.DELETE_FILE)
        assert stored[0].params == {"file_name": "update", "suffix": ".cf"}
        assert "store_action" in audit_actions(session)

    def test_action_without_file_name_is_rejected(  # noqa: ANN001
        self, auth_client: TestClient, store: Store
    ) -> None:
        empty = auth_client.post(actions_url(store.id), json={"action": "delete_file", "params": {}})

        assert empty.status_code == 422

    def test_action_rejects_traversal_and_unknown_suffix(  # noqa: ANN001
        self, auth_client: TestClient, store: Store
    ) -> None:
        traversal = auth_client.post(
            actions_url(store.id),
            json={"action": "delete_file", "params": {"file_name": "..\\1cv8"}},
        )
        bad_suffix = auth_client.post(
            actions_url(store.id),
            json={"action": "delete_file", "params": {"file_name": "update", "suffix": ".exe"}},
        )

        assert traversal.status_code == 422, "I-2: имя файла без разделителей пути"
        assert bad_suffix.status_code == 422, "удалять можно только .cf/.cfe/.xml"

    def test_file_snapshot_can_be_dropped(  # noqa: ANN001
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """Э10: `DELETE /files/{store_id}` убирает снимок — строка «не проверялся»."""
        session.add(
            FileCheck(
                store_id=store.id,
                filename="update",
                cf_exists=True,
                cfe_exists=False,
                xml_exists=False,
                cf_size=1024,
                checked_at=utcnow(),
            )
        )
        session.commit()

        response = auth_client.delete(f"/api/v1/admin/files/{store.id}?filename=update")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["removed"] == 1
        assert body["filename"] == "update"
        matrix = auth_client.get("/api/v1/admin/files/matrix?filename=update").json()
        row = next(item for item in matrix["rows"] if item["store_id"] == store.id)
        assert row["checked"] is False
        assert row["has_file"] is False
        again = auth_client.delete(f"/api/v1/admin/files/{store.id}?filename=update")
        assert again.json()["removed"] == 0
        assert "file_snapshot_delete" in audit_actions(session)
