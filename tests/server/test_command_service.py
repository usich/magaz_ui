"""Сервис задач `commands`/`job_logs` (§4.3, §4.4, I-7, §11.6).

Проверяются правила, которые видит админ на странице задач: пароль не попадает в
`params`, лог обрезается по `log_excerpt_limit_bytes`, статусы корректно
проставляют `started_at`/`finished_at`, а строки журнала получают уровень.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.db.models import JobLog, Store
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