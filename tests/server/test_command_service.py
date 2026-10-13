"""Сервис задач `commands`/`job_logs` (§4.3, §4.4, I-7, §11.6).

Проверяются правила, которые видит админ на странице задач: пароль не попадает в
`params`, лог обрезается по `log_excerpt_limit_bytes`, статусы корректно
проставляют `started_at`/`finished_at`, а строки журнала получают уровень.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from server.db.models import Command, JobLog, Store
from server.services import command_service, settings_service
from shared.enums import CommandKind, CommandStatus, JobLogLevel
from shared.timeutil import utcnow

SECRET = "пароль-1С"


def add_store(session: Session, *, name: str = "Магазин №5", active: bool = True) -> Store:
    """Магазин для задач (минимум обязательных колонок §4.1)."""
    session.rollback()
    store = Store(
        name=name,
        ip_address=f"10.10.5.{len(name)}",
        cluster_name="srv-1c",
        database_name="trade",
        version_1c="8.3.24.1548",
        user_name_1c="Администратор",
        agent_token=f"token-{len(name)}",
        is_active=active,
    )
    session.add(store)
    session.commit()
    return store


class TestSanitizeParams:
    """I-7: секреты вычищаются из `commands.params` рекурсивно."""

    def test_drops_top_level_secrets(self) -> None:
        clean = command_service.sanitize_params(
            {"file_name": "update", "password": SECRET, "token": "abc"}
        )

        assert clean == {"file_name": "update"}

    def test_drops_nested_and_case_insensitive_secrets(self) -> None:
        clean = command_service.sanitize_params(
            {"params": {"Password": SECRET, "login": "user"}, "secret_key": "x"}
        )

        assert clean == {"params": {"login": "user"}}

    def test_empty_input_returns_empty_dict(self) -> None:
        assert command_service.sanitize_params(None) == {}
        assert command_service.sanitize_params({}) == {}


class TestCreateCommand:
    """Создание задачи: статус, попытки и отсутствие секретов."""

    def test_command_starts_queued_without_secrets(self, session: Session) -> None:
        store = add_store(session)

        command = command_service.create_command(
            session,
            store_id=store.id,
            kind=CommandKind.RUN_MERGE,
            params={"file_name": "update", "password": SECRET},
            created_by="admin",
        )

        assert command.status == CommandStatus.QUEUED
        assert command.attempts == 0
        assert command.created_by == "admin"
        assert SECRET not in str(command.params)

    def test_add_log_skips_empty_lines(self, session: Session) -> None:
        store = add_store(session)
        command = command_service.create_command(
            session, store_id=store.id, kind=CommandKind.CHECK_FILES
        )

        assert command_service.add_log(session, command, "   ") is None
        assert command_service.add_log(session, command, None) is None
        entry = command_service.add_log(session, command, "файл найден")
        assert entry is not None and entry.level == JobLogLevel.INFO


class TestLogsAndExcerpt:
    """§4.3/§4.4: обрезка лога, разбор строк и уровни."""

    def test_excerpt_keeps_tail_within_limit(self, session: Session) -> None:
        settings_service.ensure_defaults(session)
        settings_service.set_value(session, "log_excerpt_limit_bytes", "1024")
        session.commit()
        store = add_store(session)
        command = command_service.create_command(
            session, store_id=store.id, kind=CommandKind.RUN_MERGE
        )

        text = "старое\n" * 500 + "Объединение конфигураций успешно завершено"
        command_service.save_excerpt(session, command, text)

        assert command.log_excerpt is not None
        assert len(command.log_excerpt) <= 1024
        assert command.log_excerpt.endswith("успешно завершено"), "хвост лога важнее начала"

    def test_log_from_text_sets_levels_and_caps_lines(self, session: Session) -> None:
        store = add_store(session)
        command = command_service.create_command(
            session, store_id=store.id, kind=CommandKind.RUN_MERGE
        )
        text = "\n".join(["шаг"] * 250 + ["Ошибка: конфликт объектов"])

        written = command_service.log_from_text(session, command, text, prefix="лог")

        entries = list(
            session.scalars(
                select(JobLog).where(JobLog.command_id == command.id).order_by(JobLog.id)
            )
        )
        assert written == command_service.MAX_LOG_LINES
        assert entries[-1].level == JobLogLevel.ERROR
        assert entries[-1].message.startswith("лог: Ошибка")
        assert entries[0].level == JobLogLevel.INFO


class TestStatusTransitions:
    """§4.3: `attempts`, `started_at`, `finished_at` и возраст задачи."""

    def test_send_and_finish_fill_timestamps(self, session: Session) -> None:
        store = add_store(session)
        command = command_service.create_command(
            session, store_id=store.id, kind=CommandKind.RUN_MERGE
        )

        command_service.set_status(session, command, CommandStatus.SENT)
        assert command.started_at is not None
        assert command.finished_at is None

        command_service.set_status(session, command, CommandStatus.RUNNING)
        command_service.set_status(session, command, CommandStatus.SUCCESS)
        assert command.finished_at is not None

    def test_attempts_are_counted_with_error(self, session: Session) -> None:
        store = add_store(session)
        command = command_service.create_command(
            session, store_id=store.id, kind=CommandKind.CHECK_FILES
        )

        first = command_service.register_attempt(session, command, error="агент недоступен")
        second = command_service.register_attempt(session, command)

        assert (first, second) == (1, 2)
        assert command.last_error == "агент недоступен", "ошибка первой попытки сохранена"

    def test_status_age_is_counted_from_start(self, session: Session) -> None:
        store = add_store(session)
        command = command_service.create_command(
            session, store_id=store.id, kind=CommandKind.RUN_MERGE
        )
        command.started_at = utcnow() - timedelta(minutes=10)

        assert command_service.status_age_sec(command) > 500


class TestQueries:
    """Выборки для страницы `/jobs` и плитки «Задач в работе» (§11.1, §11.6)."""

    def test_filters_counts_and_active(self, session: Session) -> None:
        store = add_store(session)
        first = command_service.create_command(
            session, store_id=store.id, kind=CommandKind.CHECK_FILES
        )
        second = command_service.create_command(
            session, store_id=store.id, kind=CommandKind.RUN_MERGE
        )
        command_service.set_status(session, second, CommandStatus.SUCCESS)
        session.commit()

        assert command_service.count_active(session) == 1
        assert command_service.counts_by_status(session) == {
            CommandStatus.QUEUED: 1,
            CommandStatus.SUCCESS: 1,
        }
        assert [item.id for item in command_service.list_commands(session)] == [
            second.id,
            first.id,
        ]
        assert [
            item.id for item in command_service.list_commands(session, kind="run_merge")
        ] == [second.id]
        assert [
            item.id for item in command_service.list_commands(session, active_only=True)
        ] == [first.id]
        assert [item.id for item in command_service.list_pending(session)] == [first.id]


class TestUpdateConflicts:
    """Запрет одновременного обновления одной ИБ (§9.2 FR-28, П-5) — сервисный слой."""

    def _store(
        self, session: Session, *, name: str, ip: str, cluster: str, database: str
    ) -> Store:
        """Магазин с явной серверной ИБ (`cluster_name` + `database_name`)."""
        store = Store(
            name=name,
            ip_address=ip,
            cluster_name=cluster,
            database_name=database,
            version_1c="8.3.24.1548",
            user_name_1c="Администратор",
            agent_token=f"token-{ip.replace('.', '-')}",
        )
        session.add(store)
        session.commit()
        return store

    def _job(self, session: Session, store: Store) -> Command:
        """Незавершённая задача обновления магазина (диспетчера в тестах нет)."""
        command = command_service.create_command(
            session, store_id=store.id, kind=CommandKind.RUN_MERGE, params={"file_name": "update"}
        )
        session.commit()
        return command

    def _pair(self, session: Session, *, second_cluster: str, second_database: str) -> tuple:
        """Два магазина: первый — `srv-1c\\trade`, второй — с заданными реквизитами ИБ."""
        first = self._store(session, name="Магазин №5", ip="10.10.5.10", cluster="srv-1c", database="trade")
        second = self._store(
            session, name="Магазин №6", ip="10.10.5.11", cluster=second_cluster, database=second_database
        )
        return first, second

    def test_same_store_busy_is_conflict(self, session: Session) -> None:
        store = self._store(session, name="Магазин №5", ip="10.10.5.10", cluster="srv-1c", database="trade")
        job = self._job(session, store)

        conflicts = command_service.find_update_conflicts(session, [store])

        assert [item.reason for item in conflicts] == ["same_store"]
        assert f"#{job.id}" in conflicts[0].message
        assert "слияние конфигурации" in conflicts[0].message, "видно, что именно идёт"

    def test_same_database_in_one_run_is_conflict(self, session: Session) -> None:
        first, second = self._pair(session, second_cluster="srv-1c", second_database="trade")

        conflicts = command_service.find_update_conflicts(session, [first, second])

        assert [item.reason for item in conflicts] == ["same_database_batch"]
        assert first.name in conflicts[0].message and second.name in conflicts[0].message
        assert "srv-1c\\trade" in conflicts[0].message

    def test_busy_database_of_another_store_is_conflict(self, session: Session) -> None:
        first, second = self._pair(session, second_cluster="srv-1c", second_database="trade")
        self._job(session, first)

        conflicts = command_service.find_update_conflicts(session, [second])

        assert [item.reason for item in conflicts] == ["same_database"]
        assert "уже обновляет" in conflicts[0].message
        assert first.name in conflicts[0].message

    def test_case_and_spaces_do_not_hide_same_database(self, session: Session) -> None:
        """`SRV-1C` и `srv-1c`, `Trade` и `trade` — одна ИБ (FR-28)."""
        first, second = self._pair(session, second_cluster=" SRV-1C ", second_database="Trade")

        conflicts = command_service.find_update_conflicts(session, [first, second])

        assert [item.reason for item in conflicts] == ["same_database_batch"]

    @pytest.mark.parametrize(
        ("cluster", "database"),
        [("srv-1c", "trade2"), ("srv-9c", "trade"), ("srv-1c", "")],
    )
    def test_other_database_is_not_a_conflict(
        self, session: Session, cluster: str, database: str
    ) -> None:
        """Другая ИБ или незаполненная база — запрета нет (FR-28)."""
        first, second = self._pair(session, second_cluster=cluster, second_database=database)

        assert command_service.find_update_conflicts(session, [first, second]) == []

    def test_finished_job_is_not_a_conflict(self, session: Session) -> None:
        store = self._store(session, name="Магазин №5", ip="10.10.5.10", cluster="srv-1c", database="trade")
        job = self._job(session, store)
        command_service.set_status(session, job, CommandStatus.SUCCESS)

        assert command_service.find_update_conflicts(session, [store]) == []

    def test_disabled_store_is_ignored(self, session: Session) -> None:
        """Отключённый магазин в выборку не попадает и ИБ не «занимает» (§11.4)."""
        first, second = self._pair(session, second_cluster="srv-1c", second_database="trade")
        second.is_active = False

        assert command_service.find_update_conflicts(session, [first, second]) == []


class TestDispatchGuard:
    """Критический участок «проверка R12 → создание задач» (§9.2 FR-28, П-5).

    Проверка запрета и `INSERT` задач должны быть неразрывны: иначе два админа,
    нажавшие кнопку одновременно, проскочили бы оба.
    """

    def test_guard_serializes_threads(self) -> None:
        """Второй поток не входит в участок, пока первый его не отпустил."""
        order: list[str] = []
        inside = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with command_service.dispatch_guard():
                order.append("первый вошёл")
                inside.set()
                release.wait(timeout=5)
                order.append("первый вышел")

        def second() -> None:
            inside.wait(timeout=5)
            with command_service.dispatch_guard():
                order.append("второй вошёл")

        first_thread = threading.Thread(target=holder)
        second_thread = threading.Thread(target=second)

        first_thread.start()
        assert inside.wait(timeout=5), "первый поток вошёл в участок"
        second_thread.start()
        time.sleep(0.05)  # даём второму потоку дойти до замка
        assert order == ["первый вошёл"], "пока первый держит замок, второго внутри нет"

        release.set()
        first_thread.join(timeout=5)
        second_thread.join(timeout=5)

        assert order == ["первый вошёл", "первый вышел", "второй вошёл"]

    def test_guard_is_reentrant(self) -> None:
        """Вложенный вход не блокирует сам себя (RLock: эндпоинт → хелпер)."""
        with command_service.dispatch_guard(), command_service.dispatch_guard():
            entered = True

        assert entered