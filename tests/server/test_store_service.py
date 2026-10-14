"""Тесты сервиса магазинов (§4.1, §11.2) и учётных данных 1С (§8.3, П-1)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from server.core.security import SecretBox
from server.db.models import Store
from server.services import store_service
from shared.timeutil import utcnow

#: Реквизиты магазина для тестов (как их заполняет администратор, §11.2)
STORE_FIELDS: dict[str, str] = {
    "name": "Магазин №7",
    "ip_address": "10.10.7.7",
    "cluster_name": "srv-1c",
    "database_name": "trade",
    "version_1c": "8.3.24.1548",
    "user_name_1c": "Администратор",
}


def make_store(session: Session, **overrides) -> Store:  # noqa: ANN003
    """Создать магазин через сервис — так же, как это делает API."""
    return store_service.create_store(session, **{**STORE_FIELDS, **overrides})


class TestCreateStore:
    """Создание магазина: токен выдаёт сервер, пароль только шифруется."""

    def test_generates_unique_agent_token(self, session: Session) -> None:
        first = make_store(session)
        second = make_store(session, name="Магазин №8", ip_address="10.10.8.8")
        session.commit()

        assert first.agent_token and len(first.agent_token) >= 32
        assert first.agent_token != second.agent_token
        assert first.devices_version == 1, "Д-2: версия списка оборудования стартует с 1"
        assert first.credentials_version == 1

    def test_password_is_encrypted_and_never_plaintext(
        self, session: Session, secret_box: SecretBox
    ) -> None:
        store = make_store(session, password="Секрет1С", secret_box=secret_box)
        session.commit()

        assert store.password_set is True
        assert store.password_enc != "Секрет1С"
        assert secret_box.decrypt(store.password_enc) == "Секрет1С"

    def test_password_not_set_by_default(self, session: Session) -> None:
        store = make_store(session)
        session.commit()
        assert store.password_set is False
        assert store.password_enc is None

    def test_duplicate_name_rejected_case_insensitive(self, session: Session, store: Store) -> None:
        with pytest.raises(store_service.StoreValidationError, match="уже существует"):
            make_store(session, name=store.name.lower(), ip_address="10.10.9.9")

    def test_duplicate_ip_rejected(self, session: Session, store: Store) -> None:
        with pytest.raises(store_service.StoreValidationError, match="IP"):
            make_store(session, name="Другой магазин", ip_address=store.ip_address)

    @pytest.mark.parametrize("bad_ip", ["магазин", "10.10.7", "10.10.7.999", ""])
    def test_invalid_ip_rejected(self, session: Session, bad_ip: str) -> None:
        with pytest.raises(store_service.StoreValidationError):
            make_store(session, ip_address=bad_ip)

    @pytest.mark.parametrize("bad_port", [0, 70000])
    def test_invalid_port_rejected(self, session: Session, bad_port: int) -> None:
        with pytest.raises(store_service.StoreValidationError, match="диапазоне"):
            make_store(session, agent_port=bad_port)

    def test_empty_required_field_rejected(self, session: Session) -> None:
        with pytest.raises(store_service.StoreValidationError, match="пустым"):
            make_store(session, database_name="   ")


class TestUpdateStore:
    """Изменение реквизитов (§11.2) — включая рост версии учётных данных."""

    def test_updates_fields(self, session: Session, store: Store) -> None:
        store_service.update_store(
            session,
            store,
            name="Магазин №5 (новое имя)",
            database_name="trade-new",
            version_1c="8.3.25.1",
            agent_port=8390,
            comment="перенесли кассу",
        )
        session.commit()

        assert store.name == "Магазин №5 (новое имя)"
        assert store.database_name == "trade-new"
        assert store.version_1c == "8.3.25.1"
        assert store.agent_port == 8390
        assert store.comment == "перенесли кассу"

    def test_login_change_bumps_credentials_version(self, session: Session, store: Store) -> None:
        before = store.credentials_version
        store_service.update_store(session, store, user_name_1c="ДругойПользователь")
        session.commit()

        assert store.credentials_version == before + 1, "П-1: агент должен перезапросить пароль"

    def test_same_login_does_not_bump_version(self, session: Session, store: Store) -> None:
        before = store.credentials_version
        store_service.update_store(session, store, user_name_1c=store.user_name_1c)
        session.commit()
        assert store.credentials_version == before

    def test_duplicate_ip_on_update_rejected(self, session: Session, store: Store) -> None:
        other = make_store(session, name="Магазин №9", ip_address="10.10.9.9")
        session.commit()

        with pytest.raises(store_service.StoreValidationError, match="IP"):
            store_service.update_store(session, other, ip_address=store.ip_address)

    def test_unknown_store_raises(self, session: Session) -> None:
        with pytest.raises(store_service.StoreNotFoundError):
            store_service.get_store(session, 4242)


class TestCredentials:
    """Учётные данные 1С (П-1): задать / заменить / очистить."""

    def test_set_password_and_user(
        self, session: Session, store: Store, secret_box: SecretBox
    ) -> None:
        before = store.credentials_version
        store_service.set_credentials(
            session, store, user_name_1c="ОбменДанными", password="Пароль1С", secret_box=secret_box
        )
        session.commit()

        assert store.user_name_1c == "ОбменДанными"
        assert store.password_set is True
        assert secret_box.decrypt(store.password_enc) == "Пароль1С"
        assert store.credentials_version == before + 1

    def test_replace_password(self, session: Session, store: Store, secret_box: SecretBox) -> None:
        store_service.set_credentials(session, store, password="Первый", secret_box=secret_box)
        first_enc = store.password_enc
        store_service.set_credentials(session, store, password="Второй", secret_box=secret_box)
        session.commit()

        assert store.password_enc != first_enc
        assert secret_box.decrypt(store.password_enc) == "Второй"

    def test_clear_password(self, session: Session, store: Store, secret_box: SecretBox) -> None:
        store_service.set_credentials(session, store, password="Было", secret_box=secret_box)
        store_service.set_credentials(session, store, clear_password=True)
        session.commit()

        assert store.password_set is False
        assert store.password_enc is None

    def test_version_grows_even_without_password(self, session: Session, store: Store) -> None:
        before = store.credentials_version
        store_service.set_credentials(session, store, user_name_1c="Другой")
        session.commit()
        assert store.credentials_version == before + 1


class TestTokenRotation:
    """Перевыпуск токена (§11.2): старый токен перестаёт работать."""

    def test_token_changes(self, session: Session, store: Store) -> None:
        old = store.agent_token
        new = store_service.rotate_agent_token(session, store)
        session.commit()

        assert new != old
        assert store.agent_token == new


class TestDeactivate:
    """Мягкое удаление: магазин исчезает из мониторинга (§4.1, §5.2.2)."""

    def test_store_becomes_inactive(self, session: Session, store: Store) -> None:
        store_service.deactivate_store(session, store)
        session.commit()
        assert store.is_active is False


class TestReuseAfterDeactivate:
    """Мягкое удаление освобождает имя и IP магазина (§4.1, §5.2.2).

    «Отключить магазин» — это `is_active=false`, но переиспользовать его имя и IP
    раньше было нельзя: колонки были UNIQUE по всей таблице, а проверка сервиса
    сравнивала новую запись со всеми строками, включая отключённые.
    """

    def test_ip_and_name_free_after_deactivate(self, session: Session, store: Store) -> None:
        store_service.deactivate_store(session, store)
        session.commit()

        fresh = make_store(session, name=store.name, ip_address=store.ip_address)
        session.commit()

        assert fresh.id != store.id, "старая запись остаётся в истории (§5.2.2)"
        assert fresh.is_active is True
        assert store.is_active is False

    def test_duplicate_ip_of_active_store_still_rejected(self, session: Session) -> None:
        make_store(session)
        with pytest.raises(store_service.StoreValidationError, match="IP"):
            make_store(session, name="Магазин №9", ip_address="10.10.7.7")

    def test_reactivate_conflicts_with_active_store(self, session: Session, store: Store) -> None:
        store_service.deactivate_store(session, store)
        session.commit()
        replacement = make_store(session, name=store.name, ip_address=store.ip_address)
        session.commit()

        with pytest.raises(store_service.StoreValidationError, match="уже существует"):
            store_service.update_store(session, store, is_active=True)

        assert store.is_active is False, "отказ не должен менять запись"
        assert replacement.is_active is True


class TestListing:
    """Фильтры страницы `/stores` (§11.2)."""

    def test_status_filter_uses_last_seen_window(self, session: Session, store: Store) -> None:
        store.last_seen_at = utcnow()
        other = make_store(session, name="Магазин №8", ip_address="10.10.8.8")
        other.last_seen_at = utcnow() - timedelta(seconds=50)
        session.commit()

        online = store_service.list_stores(session, status="online", online_window_sec=45)
        offline = store_service.list_stores(session, status="offline", online_window_sec=45)

        assert [item.id for item in online] == [store.id]
        assert [item.id for item in offline] == [other.id]

    def test_search_matches_name_ip_and_database(self, session: Session, store: Store) -> None:  # noqa: ARG002
        make_store(session, name="Склад", ip_address="10.10.8.8", database_name="warehouse")
        session.commit()

        assert [item.name for item in store_service.list_stores(session, search="10.10.8")] == [
            "Склад"
        ]
        assert [item.name for item in store_service.list_stores(session, search="warehouse")] == [
            "Склад"
        ]
        assert store_service.list_stores(session, search="нет-такого") == []

    def test_inactive_hidden_by_default(self, session: Session, store: Store) -> None:
        store_service.deactivate_store(session, store)
        session.commit()

        assert store_service.list_stores(session) == []
        assert len(store_service.list_stores(session, include_inactive=True)) == 1

    def test_cluster_filter(self, session: Session, store: Store) -> None:  # noqa: ARG002
        make_store(session, name="Магазин №8", ip_address="10.10.8.8", cluster_name="srv-2")
        session.commit()

        assert [item.name for item in store_service.list_stores(session, cluster="srv-2")] == [
            "Магазин №8"
        ]


class TestCounters:
    """Счётчики «Онлайн / Офлайн» для плиток главной (§11.1, FR-13)."""

    def test_counts_active_stores_only(self, session: Session, store: Store) -> None:
        fresh = make_store(session, name="Магазин №8", ip_address="10.10.8.8")
        stale = make_store(session, name="Магазин №9", ip_address="10.10.9.9")
        hidden = make_store(session, name="Магазин №10", ip_address="10.10.10.10")

        store.last_seen_at = utcnow()
        fresh.last_seen_at = utcnow() - timedelta(seconds=10)
        stale.last_seen_at = utcnow() - timedelta(seconds=300)
        hidden.is_active = False
        session.commit()

        assert store_service.count_by_status(session, online_window_sec=45) == (2, 1)

    def test_all_stores_offline_without_last_seen(self, session: Session, store: Store) -> None:  # noqa: ARG002
        assert store_service.count_by_status(session, online_window_sec=45) == (0, 1)
