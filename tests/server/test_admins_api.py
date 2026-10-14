"""Тесты API администраторов (§5.2.2, §11.6) — Д-8, состав Э6.

Проверяем то, что заявлено в §13.2: список, создание, смена пароля, блокировка
и удаление; хеш пароля (и сам пароль) не появляются ни в ответах API, ни в
аудите. Отдельно — два запрета из `admin_service`: себя и последнего
действующего администратора трогать нельзя.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from server.db.models import AdminUser, AuditLog
from server.services import admin_service
from tests.server.conftest import ADMIN_PASSWORD, ADMIN_USERNAME

ADMINS_URL = "/api/v1/admin/admins"
LOGIN_URL = "/api/v1/admin/auth/login"
ME_URL = "/api/v1/admin/auth/me"
AUDIT_URL = "/api/v1/admin/audit"

OTHER_USERNAME = "operator"
OTHER_PASSWORD = "operator-password-2026"


def admin_rows(engine: Engine) -> list[tuple[str, bool]]:
    """Логины и признак блокировки — снимок из отдельной сессии (свежие данные)."""
    with Session(engine) as check:
        rows = check.scalars(select(AdminUser).order_by(AdminUser.id)).all()
        return [(row.username, row.is_active) for row in rows]


def audit_actions(engine: Engine) -> list[str]:
    """Действия аудита, от свежих к старым."""
    with Session(engine) as check:
        rows = check.scalars(select(AuditLog).order_by(AuditLog.id.desc())).all()
        return [row.action for row in rows]


def audit_details_text(engine: Engine) -> str:
    """Все `details` аудита одной строкой — для проверки «пароля здесь нет»."""
    with Session(engine) as check:
        rows = check.scalars(select(AuditLog)).all()
        return json.dumps([row.details for row in rows], ensure_ascii=False)


def create_admin_via_api(client: TestClient, *, username: str = OTHER_USERNAME, password: str = OTHER_PASSWORD):
    """Создать администратора через API (обычный путь администратора в UI)."""
    response = client.post(ADMINS_URL, json={"username": username, "password": password})
    assert response.status_code == 201, response.text
    return response


class TestAdminsList:
    """Список администраторов доступен только вошедшему администратору."""

    def test_requires_authentication(self, client: TestClient) -> None:
        assert client.get(ADMINS_URL).status_code == 401

    def test_lists_admins_without_password_hash(
        self, auth_client: TestClient, admin  # noqa: ANN001, ARG002
    ) -> None:
        response = auth_client.get(ADMINS_URL)
        body = response.json()

        assert response.status_code == 200
        assert [item["username"] for item in body] == [ADMIN_USERNAME]
        assert body[0]["id"] == admin.id
        assert body[0]["is_active"] is True
        assert body[0]["last_login_at"], "вход в дашборд уже был (фикстура auth_client)"
        assert "password" not in response.text.lower()
        assert "password_hash" not in response.text


class TestAdminCreate:
    """Д-8: создание администратора с проверкой логина и длины пароля."""

    def test_creates_admin_and_allows_login(
        self, auth_client: TestClient, admin, engine: Engine  # noqa: ANN001, ARG002
    ) -> None:
        response = create_admin_via_api(auth_client)
        body = response.json()

        assert body["username"] == OTHER_USERNAME
        assert body["is_active"] is True
        assert admin_rows(engine) == [(ADMIN_USERNAME, True), (OTHER_USERNAME, True)]
        assert audit_actions(engine)[0] == "admin_create"

        login = auth_client.post(
            LOGIN_URL, json={"username": OTHER_USERNAME, "password": OTHER_PASSWORD}
        )
        assert login.status_code == 200, "новый администратор входит своим паролем"

    def test_duplicate_username_is_409(
        self, auth_client: TestClient, admin  # noqa: ANN001, ARG002
    ) -> None:
        response = auth_client.post(
            ADMINS_URL, json={"username": ADMIN_USERNAME.upper(), "password": OTHER_PASSWORD}
        )

        assert response.status_code == 409
        assert "уже существует" in response.text

    def test_short_password_is_422_and_nothing_created(
        self, auth_client: TestClient, admin, engine: Engine  # noqa: ANN001, ARG002
    ) -> None:
        response = auth_client.post(ADMINS_URL, json={"username": OTHER_USERNAME, "password": "short"})

        assert response.status_code == 422
        assert "символов" in response.text
        assert admin_rows(engine) == [(ADMIN_USERNAME, True)]
        assert "admin_create" not in audit_actions(engine)

    def test_empty_username_is_422(self, auth_client: TestClient, admin) -> None:  # noqa: ANN001, ARG002
        response = auth_client.post(ADMINS_URL, json={"username": "  ", "password": OTHER_PASSWORD})
        assert response.status_code == 422

    def test_password_is_not_returned_nor_audited(
        self, auth_client: TestClient, admin, engine: Engine  # noqa: ANN001, ARG002
    ) -> None:
        response = create_admin_via_api(auth_client)
        assert OTHER_PASSWORD not in response.text

        audit = auth_client.get(AUDIT_URL, params={"limit": 50})
        assert OTHER_PASSWORD not in audit.text
        assert OTHER_PASSWORD not in audit_details_text(engine)
        assert audit.json()[0]["details"] == {"username": OTHER_USERNAME}


class TestAdminUpdate:
    """Д-8: смена пароля и блокировка (`PUT /admins/{id}`)."""

    def test_changes_password_of_another_admin(
        self, auth_client: TestClient, admin, engine: Engine  # noqa: ANN001, ARG002
    ) -> None:
        created = create_admin_via_api(auth_client).json()
        new_password = "operator-password-new-1"

        response = auth_client.put(
            f"{ADMINS_URL}/{created['id']}", json={"password": new_password}
        )

        assert response.status_code == 200
        assert response.json()["username"] == OTHER_USERNAME
        assert audit_actions(engine)[0] == "admin_password_change"
        assert new_password not in response.text
        assert new_password not in audit_details_text(engine)

        # Старый пароль больше не работает, новый — работает
        assert (
            auth_client.post(
                LOGIN_URL, json={"username": OTHER_USERNAME, "password": OTHER_PASSWORD}
            ).status_code
            == 401
        )
        assert (
            auth_client.post(
                LOGIN_URL, json={"username": OTHER_USERNAME, "password": new_password}
            ).status_code
            == 200
        )

    def test_changes_own_password_and_keeps_session(
        self, auth_client: TestClient, admin, engine: Engine  # noqa: ANN001, ARG002
    ) -> None:
        new_password = "admin-password-new-1"
        response = auth_client.put(
            f"{ADMINS_URL}/{admin.id}", json={"password": new_password}
        )

        assert response.status_code == 200
        assert "admin_password_change" in audit_actions(engine)
        assert auth_client.get(ME_URL).status_code == 200, "сессия на cookie продолжает жить (§8.2)"
        assert (
            auth_client.post(
                LOGIN_URL, json={"username": ADMIN_USERNAME, "password": new_password}
            ).status_code
            == 200
        ), "новый пароль работает"
        assert (
            auth_client.post(
                LOGIN_URL, json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
            ).status_code
            == 401
        ), "старый пароль больше не работает"

    def test_blocks_and_unblocks_another_admin(
        self, auth_client: TestClient, admin, engine: Engine  # noqa: ANN001, ARG002
    ) -> None:
        created = create_admin_via_api(auth_client).json()

        blocked = auth_client.put(f"{ADMINS_URL}/{created['id']}", json={"is_active": False})
        assert blocked.status_code == 200
        assert blocked.json()["is_active"] is False
        assert admin_rows(engine) == [(ADMIN_USERNAME, True), (OTHER_USERNAME, False)]
        assert audit_actions(engine)[0] == "admin_block"
        assert (
            auth_client.post(
                LOGIN_URL, json={"username": OTHER_USERNAME, "password": OTHER_PASSWORD}
            ).status_code
            == 401
        ), "заблокированный администратор не входит"

        unblocked = auth_client.put(f"{ADMINS_URL}/{created['id']}", json={"is_active": True})
        assert unblocked.status_code == 200
        assert unblocked.json()["is_active"] is True
        assert audit_actions(engine)[0] == "admin_unblock"
        assert (
            auth_client.post(
                LOGIN_URL, json={"username": OTHER_USERNAME, "password": OTHER_PASSWORD}
            ).status_code
            == 200
        )

    def test_blocked_admin_cannot_use_issued_cookie(
        self, auth_client: TestClient, admin, engine: Engine  # noqa: ANN001, ARG002
    ) -> None:
        created = create_admin_via_api(auth_client).json()
        assert (
            auth_client.post(
                LOGIN_URL, json={"username": OTHER_USERNAME, "password": OTHER_PASSWORD}
            ).status_code
            == 200
        )
        operator_cookie = auth_client.cookies.get("magaz_admin")
        assert operator_cookie, "cookie оператора получена"

        auth_client.post(
            LOGIN_URL, json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
        )
        assert auth_client.put(
            f"{ADMINS_URL}/{created['id']}", json={"is_active": False}
        ).status_code == 200

        # Возвращаем cookie, выданную до блокировки: сервер обязан её отклонить
        auth_client.cookies.clear()
        auth_client.cookies.set("magaz_admin", operator_cookie)
        assert auth_client.get(ME_URL).status_code == 401

    def test_cannot_block_self(self, auth_client: TestClient, admin, engine: Engine) -> None:  # noqa: ANN001, ARG002
        response = auth_client.put(f"{ADMINS_URL}/{admin.id}", json={"is_active": False})

        assert response.status_code == 403
        assert "свою учётную запись" in response.text
        assert admin_rows(engine) == [(ADMIN_USERNAME, True)]
        assert auth_client.get(ME_URL).status_code == 200, "доступ не потерян"

    def test_short_password_is_422(
        self, auth_client: TestClient, admin, engine: Engine  # noqa: ANN001, ARG002
    ) -> None:
        created = create_admin_via_api(auth_client).json()
        response = auth_client.put(f"{ADMINS_URL}/{created['id']}", json={"password": "short"})

        assert response.status_code == 422
        assert "admin_password_change" not in audit_actions(engine)

    def test_empty_update_is_422(self, auth_client: TestClient, admin) -> None:  # noqa: ANN001, ARG002
        response = auth_client.put(f"{ADMINS_URL}/{admin.id}", json={})
        assert response.status_code == 422

    def test_unknown_admin_is_404(self, auth_client: TestClient, admin) -> None:  # noqa: ANN001, ARG002
        assert auth_client.put(f"{ADMINS_URL}/9999", json={"is_active": False}).status_code == 404

    def test_requires_authentication(self, client: TestClient, admin) -> None:  # noqa: ANN001, ARG002
        assert client.put(f"{ADMINS_URL}/{admin.id}", json={"is_active": False}).status_code == 401
        assert client.delete(f"{ADMINS_URL}/{admin.id}").status_code == 401


class TestAdminDelete:
    """Д-8: удаление учётной записи администратора."""

    def test_deletes_another_admin(
        self, auth_client: TestClient, admin, engine: Engine  # noqa: ANN001, ARG002
    ) -> None:
        created = create_admin_via_api(auth_client).json()

        response = auth_client.delete(f"{ADMINS_URL}/{created['id']}")

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert admin_rows(engine) == [(ADMIN_USERNAME, True)]
        assert [item["username"] for item in auth_client.get(ADMINS_URL).json()] == [ADMIN_USERNAME]
        assert audit_actions(engine)[0] == "admin_delete"
        assert (
            auth_client.post(
                LOGIN_URL, json={"username": OTHER_USERNAME, "password": OTHER_PASSWORD}
            ).status_code
            == 401
        ), "удалённый администратор не входит"

    def test_cannot_delete_self(self, auth_client: TestClient, admin, engine: Engine) -> None:  # noqa: ANN001, ARG002
        response = auth_client.delete(f"{ADMINS_URL}/{admin.id}")

        assert response.status_code == 403
        assert admin_rows(engine) == [(ADMIN_USERNAME, True)]
        assert auth_client.get(ME_URL).status_code == 200

    def test_unknown_admin_is_404(self, auth_client: TestClient, admin) -> None:  # noqa: ANN001, ARG002
        assert auth_client.delete(f"{ADMINS_URL}/4242").status_code == 404

    def test_audit_keeps_history_of_deleted_admin(
        self, auth_client: TestClient, admin, engine: Engine  # noqa: ANN001, ARG002
    ) -> None:
        created = create_admin_via_api(auth_client).json()
        auth_client.post(
            LOGIN_URL, json={"username": OTHER_USERNAME, "password": OTHER_PASSWORD}
        )
        auth_client.post(
            LOGIN_URL, json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
        )
        assert auth_client.delete(f"{ADMINS_URL}/{created['id']}").status_code == 200

        # Аудит ссылается на логин строкой, поэтому история удалённого остаётся
        entries = auth_client.get(AUDIT_URL, params={"limit": 50}).json()
        assert any(item["actor"] == OTHER_USERNAME for item in entries)
        assert "admin_delete" in [item["action"] for item in entries]


class TestAdminServiceGuards:
    """Запреты, которые живут в `admin_service`, а не в оформлении ответа."""

    def test_last_active_admin_cannot_be_blocked_or_deleted(self, session: Session) -> None:
        solo = admin_service.create_admin(
            session, username="solo", password="solo-password-1"
        )
        session.commit()

        with pytest.raises(admin_service.LastActiveAdminError):
            admin_service.set_active(session, solo, is_active=False)
        with pytest.raises(admin_service.LastActiveAdminError):
            admin_service.delete_admin(session, solo)

        assert admin_service.count_active(session) == 1

    def test_self_guard_blocks_own_account_but_allows_others(self, session: Session) -> None:
        first = admin_service.create_admin(session, username="first", password="first-password-1")
        admin_service.create_admin(session, username="second", password="second-password-1")
        session.commit()

        with pytest.raises(admin_service.AdminSelfError):
            admin_service.set_active(session, first, is_active=False, actor_id=first.id)

        # Блокировка «соседа» проходит: действующих администраторов было двое
        second = admin_service.get_by_username(session, "second")
        assert second is not None
        admin_service.set_active(session, second, is_active=False, actor_id=first.id)
        assert admin_service.list_admins(session)[0].username == "first", "действующие первыми"


