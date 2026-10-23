"""API локальных баз 1С (Д-20) — CRUD, группировка по `group`, учётные данные 1С.

Проверяется контракт точек `/api/v1/admin/local-bases/*`: создание, правка,
мягкое удаление, уникальность имени, пароль 1С (наружу уходит только
`password_set`), и фильтр/сортировка по группе для группировки в админке.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from server.db.models import AuditLog, LocalBase

BASE_URL = "/api/v1/admin/local-bases"


def audit_actions(session: Session) -> list[str]:
    """Действия аудита в порядке появления (§4.10)."""
    session.expire_all()
    return list(session.scalars(select(AuditLog.action).order_by(AuditLog.id)))


def bases(session: Session) -> list[LocalBase]:
    """Все локальные базы из БД в порядке появления."""
    session.expire_all()
    return list(session.scalars(select(LocalBase).order_by(LocalBase.id)))


def make_payload(**overrides) -> dict:
    """Тело POST локальной базы (реквизиты подключения + группа)."""
    payload = {
        "name": "Бухгалтерия",
        "version_1c": "8.3.24.1548",
        "cluster_name": "srv-1c",
        "database_name": "buh",
        "user_name_1c": "Администратор",
        "base_path": r"C:\1C\buh",
        "extension_name": "БГ_Бухгалтерия",
        "group": "Финансы",
    }
    payload.update(overrides)
    return payload


def test_create_and_list_with_group(auth_client: TestClient, session: Session) -> None:
    """Создание, пароль наружу не уходит, в списке обе базы одной группы."""
    response = auth_client.post(
        BASE_URL,
        json=make_payload(password="секрет-пароль"),
    )
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["name"] == "Бухгалтерия"
    assert created["group"] == "Финансы"
    assert created["password_set"] is True
    assert "password" not in created
    assert "секрет-пароль" not in response.text
    assert created["base_path"] == r"C:\1C\buh"

    # вторая база той же группы
    second = auth_client.post(
        BASE_URL,
        json=make_payload(name="Бухгалтерия фил.", database_name="buh2"),
    )
    assert second.status_code == 200, second.text

    listed = auth_client.get(BASE_URL).json()
    assert [row["name"] for row in listed] == ["Бухгалтерия", "Бухгалтерия фил."]
    assert {row["group"] for row in listed} == {"Финансы"}

    # фильтр по группе
    filtered = auth_client.get(BASE_URL, params={"group": "Финансы"}).json()
    assert len(filtered) == 2
    no_match = auth_client.get(BASE_URL, params={"group": "Другая"}).json()
    assert no_match == []

    # в начале аудита всегда стоит admin_login от фикстуры входа — смотрим хвост
    assert audit_actions(session)[-2:] == ["local_base_create", "local_base_create"]


def test_group_sections_sorted(auth_client: TestClient, session: Session) -> None:
    """Базы разных групп приходят упорядоченными по группе (для секций админки)."""
    auth_client.post(BASE_URL, json=make_payload(name="Зарплата", group="Кадры"))
    auth_client.post(BASE_URL, json=make_payload(name="Торговля", group="Продажи"))
    auth_client.post(BASE_URL, json=make_payload(name="Бухгалтерия", group="Финансы"))

    listed = auth_client.get(BASE_URL).json()
    assert [(row["group"], row["name"]) for row in listed] == [
        ("Кадры", "Зарплата"),
        ("Продажи", "Торговля"),
        ("Финансы", "Бухгалтерия"),
    ]


def test_update_and_deactivate(auth_client: TestClient, session: Session) -> None:
    """Правка реквизитов, смена логина поднимает credentials_version, мягкое удаление."""
    created = auth_client.post(BASE_URL, json=make_payload(password="пароль")).json()

    updated = auth_client.put(
        f"{BASE_URL}/{created['id']}",
        json={"group": "Отчётность", "version_1c": "8.3.25.1000"},
    ).json()
    assert updated["group"] == "Отчётность"
    assert updated["version_1c"] == "8.3.25.1000"
    assert updated["credentials_version"] == 1

    cred = auth_client.put(
        f"{BASE_URL}/{created['id']}/credentials",
        json={"user_name_1c": "Главбух", "password": "новый"},
    ).json()
    assert cred["credentials_version"] == 2
    assert cred["user_name_1c"] == "Главбух"
    assert cred["password_set"] is True
    assert "новый" not in cred

    deactivated = auth_client.delete(f"{BASE_URL}/{created['id']}").json()
    assert deactivated["is_active"] is False

    listed = auth_client.get(BASE_URL).json()
    assert listed == []

    # в начале аудита всегда стоит admin_login от фикстуры входа — смотрим хвост
    assert audit_actions(session)[-4:] == [
        "local_base_create",
        "local_base_update",
        "local_base_credentials_update",
        "local_base_deactivate",
    ]


def test_duplicate_active_name_conflict(auth_client: TestClient) -> None:
    """Активная база занимает имя: повторное создание того же имени — `422`."""
    auth_client.post(BASE_URL, json=make_payload(name="Бухгалтерия"))
    dup = auth_client.post(
        BASE_URL,
        json=make_payload(name="бухгалтерия", database_name="other"),
    )
    assert dup.status_code == 422
    assert "уже существует" in dup.text


def test_credentials_clear_password(auth_client: TestClient, session: Session) -> None:
    """Очистка пароля через credentials: признак уходит в `false`."""
    created = auth_client.post(BASE_URL, json=make_payload(password="пароль")).json()
    cleared = auth_client.put(
        f"{BASE_URL}/{created['id']}/credentials",
        json={"clear_password": True},
    ).json()
    assert cleared["password_set"] is False
    assert cleared["credentials_version"] == 2


def test_missing_required_fields(auth_client: TestClient) -> None:
    """Обязательные реквизиты пустыми быть не могут — `422`."""
    response = auth_client.post(BASE_URL, json=make_payload(cluster_name=""))
    assert response.status_code == 422
    assert "Кластер" in response.text
