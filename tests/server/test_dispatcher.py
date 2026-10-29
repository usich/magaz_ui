"""Диспетчер операций: очередь, retry, опрос `/check-*`, П-1 (§3.2, §13.2).

Главная проверка здесь — «эталонный сценарий» §12 Э4: скачать → проверить →
merge → update с корректными статусами и логом, а также правила работы с
офлайн-магазинами и доставкой учётных данных 1С.

Сеть подменяется `ScriptedAgent` (мок агента), поэтому ни кассы, ни туннеля для
проверок не требуется (§13: «все внешние вызовы инжектируются»).
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from server.config import Settings
from server.core.client_http import AgentProtocolError
from server.core.dispatcher import CommandDispatcher, build_1c_payload
from server.core.security import SecretBox
from server.db.models import Command, FileCheck, JobLog, Notification, Store
from server.db.session import DatabaseManager
from server.services import (
    command_service,
    device_service,
    notification_service,
    settings_service,
)
from shared.enums import CommandKind, CommandStatus
from shared.timeutil import utcnow
from tests.server.agent_mock import (
    ScriptedAgent,
    health_body,
    status_transport,
    timeout_transport,
    transport_from_handler,
)

#: Пароль 1С в тестах — им же проверяется, что он нигде не «утекает» (I-7)
STORE_PASSWORD = "S3cret-1C-password"


@pytest.fixture()
def manager(settings: Settings, engine) -> Iterator[DatabaseManager]:  # noqa: ANN001, ARG001
    """Менеджер БД на той же временной базе, что и тестовая сессия."""
    db = DatabaseManager(settings.database_url)
    yield db
    db.dispose()


def build_dispatcher(
    manager: DatabaseManager,
    settings: Settings,
    *,
    transport,
    secret_box: SecretBox | None = None,
    backoff_base_sec: float = 0.0,
    busy_grace_sec: float = 0.0,
) -> CommandDispatcher:
    """Диспетчер для тестов: без реальных пауз backoff-а и «льготного» ожидания 1С."""
    return CommandDispatcher(
        manager,
        settings,
        transport=transport,
        secret_box=secret_box,
        interval_override_sec=0.05,
        backoff_base_sec=backoff_base_sec,
        busy_grace_sec=busy_grace_sec,
    )


def seed_settings(session: Session, **values: str) -> None:
    """Подсеять настройки §4.9 (в остальном берутся значения по умолчанию)."""
    settings_service.ensure_defaults(session)
    for key, value in values.items():
        settings_service.set_value(session, key, value)
    session.commit()


def make_store(
    session: Session,
    *,
    name: str = "Магазин №5",
    online: bool = True,
    password: str | None = None,
    credentials_version: int = 1,
    devices_version: int = 1,
    secret_box: SecretBox | None = None,
    is_active: bool = True,
    extension_name: str | None = None,
) -> Store:
    """Магазин с настраиваемым «онлайн»-состоянием и учётными данными 1С (П-1)."""
    store = Store(
        name=name,
        ip_address="10.10.5.10",
        cluster_name="srv-1c",
        database_name="trade",
        version_1c="8.3.24.1548",
        user_name_1c="Администратор",
        extension_name=extension_name,
        # Токен агента — только ASCII: он уходит в заголовок `X-Agent-Token` (§8.1)
        agent_token=f"token-{uuid4().hex[:12]}",
        credentials_version=credentials_version,
        devices_version=devices_version,
        password_enc=(secret_box.encrypt(password) if password and secret_box else None),
        last_seen_at=utcnow() if online else None,
        is_active=is_active,
    )
    session.add(store)
    session.commit()
    return store


def add_command(
    session: Session, store: Store, kind: CommandKind, params: dict | None = None
) -> Command:
    """Поставить задачу так же, как это делает админский API (§5.2.2)."""
    command = command_service.create_command(
        session, store_id=store.id, kind=kind, params=params, created_by="admin"
    )
    session.commit()
    return command


def fresh(session: Session, command_id: int) -> Command:
    """Перечитать задачу из БД (диспетчер пишет своей сессией).

    `expunge_all()` + `rollback()` нужны по двум причинам: снять с тестовой сессии
    закешированные объекты (`expire_on_commit=False` держит их «как были») и
    закрыть возможную читающую транзакцию — SQLite блокирует файл целиком.
    """
    session.expunge_all()
    session.rollback()
    command = session.get(Command, command_id)
    assert command is not None
    return command


class TestQueueForOfflineStores:
    """FR-16: касса офлайн — команда ждёт в очереди и уходит при появлении в сети."""

    async def test_offline_store_keeps_command_queued(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        seed_settings(session)
        store = make_store(session, online=False)
        command = add_command(session, store, CommandKind.CHECK_FILES, {"filename": "update"})

        dispatcher = build_dispatcher(manager, settings, transport=timeout_transport())
        report = await dispatcher.process_once()

        state = fresh(session, command.id)
        assert state.status == CommandStatus.QUEUED, "офлайн — команда остаётся в очереди"
        assert state.attempts == 0, "ожидание кассы в сети — не «попытка отправки»"
        assert report.waiting == 1
        assert report.sent == 0

    async def test_command_goes_out_when_store_appears(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        seed_settings(session)
        store = make_store(session, online=False)
        command = add_command(session, store, CommandKind.CHECK_FILES, {"filename": "update"})
        agent = ScriptedAgent()

        offline = build_dispatcher(manager, settings, transport=timeout_transport())
        await offline.process_once()
        assert fresh(session, command.id).status == CommandStatus.QUEUED

        store.last_seen_at = utcnow()  # касса вернулась в сеть (§3.3)
        session.commit()

        online = build_dispatcher(manager, settings, transport=agent.transport)
        await online.process_once()

        assert "/check-files" in agent.paths()
        assert fresh(session, command.id).status == CommandStatus.SUCCESS

    async def test_inactive_store_cancels_command(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        seed_settings(session)
        store = make_store(session, online=False, is_active=False)
        command = add_command(session, store, CommandKind.CHECK_FILES, {"filename": "update"})

        dispatcher = build_dispatcher(manager, settings, transport=timeout_transport())
        report = await dispatcher.process_once()

        assert fresh(session, command.id).status == CommandStatus.CANCELED
        assert report.failed == 1


class TestRetryWithBackoff:
    """§13.2: retry с backoff и лимит попыток (`command_retry_max`)."""

    async def test_retry_is_delayed_by_backoff(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        seed_settings(session, command_retry_max="3")
        store = make_store(session, online=True)
        command = add_command(
            session, store, CommandKind.SEND_MESSAGE, {"text_message": "Обновление через 5 минут"}
        )

        dispatcher = build_dispatcher(
            manager, settings, transport=status_transport(500), backoff_base_sec=60.0
        )
        first = await dispatcher.process_once()

        state = fresh(session, command.id)
        assert state.attempts == 1
        assert state.status == CommandStatus.QUEUED, "после сбоя задача ждёт повтора"
        assert "500" in (state.last_error or "")
        assert first.retried == 1

        second = await dispatcher.process_once()
        assert second.sent == 0, "backoff не даёт долбить кассу каждые 5 секунд"
        assert fresh(session, command.id).attempts == 1

    async def test_attempt_limit_closes_command_as_error(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        seed_settings(session, command_retry_max="2")
        store = make_store(session, online=True)
        command = add_command(
            session, store, CommandKind.SEND_MESSAGE, {"text_message": "Тест"}
        )

        dispatcher = build_dispatcher(
            manager, settings, transport=status_transport(500), backoff_base_sec=0.0
        )
        await dispatcher.process_once()
        await dispatcher.process_once()

        state = fresh(session, command.id)
        assert state.attempts == 2
        assert state.status == CommandStatus.ERROR
        assert "2 попыток" in (state.last_error or "")


class TestReferenceScenario:
    """§12 Э4: «скачать → проверить → merge → update» с корректными статусами и логом."""

    async def test_merge_202_then_200_success(
        self,
        manager: DatabaseManager,
        settings: Settings,
        session: Session,
        secret_box: SecretBox,
    ) -> None:
        seed_settings(session)
        store = make_store(
            session,
            online=True,
            password=STORE_PASSWORD,
            credentials_version=1,
            secret_box=secret_box,
        )
        command = add_command(
            session,
            store,
            CommandKind.RUN_MERGE,
            {"file_name": "update", "full_merge": False},
        )
        agent = ScriptedAgent(checks_before_success=2)
        dispatcher = build_dispatcher(
            manager, settings, transport=agent.transport, secret_box=secret_box
        )

        first = await dispatcher.process_once()
        assert first.sent == 1
        state = fresh(session, command.id)
        assert state.status == CommandStatus.SENT
        assert state.log_files == ["log-merge.txt"], "имя лога — как в v2 (§7.1)"

        await dispatcher.process_once()  # 202: операция идёт
        assert fresh(session, command.id).status == CommandStatus.RUNNING

        await dispatcher.process_once()  # 200: успех
        state = fresh(session, command.id)
        assert state.status == CommandStatus.SUCCESS
        assert state.finished_at is not None
        assert "Объединение конфигураций успешно завершено" in (state.log_excerpt or "")

        messages = [
            entry.message
            for entry in session.scalars(
                select(JobLog).where(JobLog.command_id == command.id).order_by(JobLog.id)
            )
        ]
        assert any("успешно завершено" in message for message in messages), "лог в job_logs"

    async def test_merge_payload_has_no_password(
        self,
        manager: DatabaseManager,
        settings: Settings,
        session: Session,
        secret_box: SecretBox,
    ) -> None:
        """I-7/П-1: пароль не уходит в команду, не лежит в задаче и не виден в теле HTTP."""
        seed_settings(session)
        store = make_store(
            session,
            online=True,
            password=STORE_PASSWORD,
            credentials_version=7,
            secret_box=secret_box,
        )
        command = add_command(
            session,
            store,
            CommandKind.RUN_MERGE,
            {"file_name": "update", "password": "от-админа"},
        )
        agent = ScriptedAgent(credentials_version=7)
        dispatcher = build_dispatcher(
            manager, settings, transport=agent.transport, secret_box=secret_box
        )
        await dispatcher.process_once()

        body = next(item["json"] for item in agent.requests if item["path"] == "/run-merge")
        assert body["server_name"] == "srv-1c"
        assert body["base_name"] == "trade"
        assert body["version_1c"] == "8.3.24.1548"
        assert body["user_name"] == "Администратор"
        assert "password" not in body

        state = fresh(session, command.id)
        assert "password" not in (state.params or {})
        assert STORE_PASSWORD not in json.dumps(agent.json_bodies(), ensure_ascii=False)
        assert (
            STORE_PASSWORD
            not in json.dumps(
                [stored.params for stored in command_service.list_commands(session)],
                ensure_ascii=False,
            )
        ), "в истории задач пароля нет (§4.3)"

    async def test_merge_failure_sets_failed_with_log(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        seed_settings(session)
        store = make_store(session, online=True)
        command = add_command(session, store, CommandKind.RUN_MERGE, {"file_name": "update"})
        agent = ScriptedAgent(fail_operation=True)
        dispatcher = build_dispatcher(manager, settings, transport=agent.transport)

        await dispatcher.process_once()
        await dispatcher.process_once()

        state = fresh(session, command.id)
        assert state.status == CommandStatus.FAILED
        assert state.last_error == "Merge failed."
        assert "Ошибка" in (state.log_excerpt or "")
        assert state.finished_at is not None

    async def test_update_db_uses_check_update_db(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        seed_settings(session)
        store = make_store(session, online=True)
        command = add_command(
            session, store, CommandKind.RUN_UPDATE_DB, {"is_fast_update": False}
        )
        agent = ScriptedAgent()
        dispatcher = build_dispatcher(manager, settings, transport=agent.transport)

        await dispatcher.process_once()
        await dispatcher.process_once()

        assert "/run-update-db" in agent.paths()
        assert "/check-update-db" in agent.paths()
        body = next(item["json"] for item in agent.requests if item["path"] == "/run-update-db")
        assert body["is_fast_update"] is False
        assert fresh(session, command.id).status == CommandStatus.SUCCESS

    async def test_update_db_from_store_card_polls_with_extension_flag(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        """FR-4: `/check-update-db` обязан опрашивать расширение с `extension=true`.

        Когда имя расширения живёт в карточке магазина, а в `params` операции его
        нет, сервер всё равно отправляет агента на расширение (как в
        `build_1c_payload`). Если бы `/check-update-db` вызывался без
        `extension=true`, агент читал бы `log-update-db.txt`, а команда пишет в
        `log-update-db-extension.txt` — сервер не нашёл бы результат (задача
        «не может прочитать файл лога»).
        """
        seed_settings(session)
        store = make_store(session, online=True, extension_name="БГ_Торговоя")
        command = add_command(
            session, store, CommandKind.RUN_UPDATE_DB, {"is_fast_update": False}
        )
        agent = ScriptedAgent()
        dispatcher = build_dispatcher(manager, settings, transport=agent.transport)

        await dispatcher.process_once()
        await dispatcher.process_once()

        check_calls = [
            item["query"] for item in agent.requests if item["path"] == "/check-update-db"
        ]
        assert check_calls, "агент должен опрашивать /check-update-db"
        assert all(item.get("extension") == "true" for item in check_calls)
        assert fresh(session, command.id).status == CommandStatus.SUCCESS

    async def test_merge_cf_file_polls_without_extension_flag(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        """Д-16: `.cf`-имя файла → опрос `/check-merge` без `?extension=true`.

        Даже когда в карточке магазина прописано расширение, явный `.cf` в имени
        файла означает слияние основной конфигурации: флаг опроса обязан быть
        `false`, иначе агент читал бы `log-merge-extension.txt`, а команда пишет
        в `log-merge.txt`.
        """
        seed_settings(session)
        store = make_store(session, online=True, extension_name="БГ_Торговоя")
        command = add_command(
            session, store, CommandKind.RUN_MERGE, {"file_name": "1cv8.cf"}
        )
        agent = ScriptedAgent()
        dispatcher = build_dispatcher(manager, settings, transport=agent.transport)

        await dispatcher.process_once()
        await dispatcher.process_once()

        check_calls = [
            item["query"] for item in agent.requests if item["path"] == "/check-merge"
        ]
        assert check_calls, "агент должен опрашивать /check-merge"
        assert all(item.get("extension") == "false" for item in check_calls)
        assert fresh(session, command.id).status == CommandStatus.SUCCESS

    async def test_full_reference_chain_download_check_merge_update(
        self,
        manager: DatabaseManager,
        settings: Settings,
        session: Session,
        secret_box: SecretBox,
    ) -> None:
        """DoD Э4: «скачать → проверить → merge → update» на моке агента.

        Сценарий идёт так же, как его выполняет админ (§3.3): сначала скачивание
        пакета, затем проверка файлов, потом слияние и обновление БД. Каждый шаг
        дожидается итогового статуса, потому что сервер опрашивает кассу
        (`/check-*`) и только затем закрывает задачу.
        """
        seed_settings(session)
        store = make_store(
            session, online=True, password=STORE_PASSWORD, secret_box=secret_box
        )
        agent = ScriptedAgent()
        dispatcher = build_dispatcher(
            manager, settings, transport=agent.transport, secret_box=secret_box
        )

        async def wait_for(command_id: int, expected: str, limit: int = 12) -> Command:
            for _ in range(limit):
                if fresh(session, command_id).status == expected:
                    return fresh(session, command_id)
                await dispatcher.process_once()
            return fresh(session, command_id)

        download = add_command(
            session,
            store,
            CommandKind.DOWNLOAD_FILE,
            {"url-file": "https://disk.yandex.ru/d/XXXX/update.cf"},
        )
        assert (await wait_for(download.id, CommandStatus.SUCCESS)).status == CommandStatus.SUCCESS

        # §11.3: после скачивания сервер сам заказал повторную проверку
        follow_ups = command_service.list_commands(session, kind="check_files")
        assert follow_ups, "повторная проверка создана автоматически"
        assert (await wait_for(follow_ups[0].id, CommandStatus.SUCCESS)).status == (
            CommandStatus.SUCCESS
        )

        merge = add_command(session, store, CommandKind.RUN_MERGE, {"file_name": "update"})
        merge_state = await wait_for(merge.id, CommandStatus.SUCCESS)
        assert "Объединение конфигураций успешно завершено" in (merge_state.log_excerpt or "")

        update = add_command(
            session, store, CommandKind.RUN_UPDATE_DB, {"is_fast_update": False}
        )
        update_state = await wait_for(update.id, CommandStatus.SUCCESS)
        assert "Обновление конфигурации успешно завершено" in (update_state.log_excerpt or "")
        assert update_state.log_files == ["log-update-db.txt"]

        # I-7: пароль не встречается ни в одном теле запроса к кассе
        assert STORE_PASSWORD not in json.dumps(agent.json_bodies(), ensure_ascii=False)
        assert "/credentials" not in agent.paths(), "версии совпали — push не нужен"
        # Д-18: после полного обновления создаётся notify-задача «Можно входить»
        # — она в очереди, а не завершена в этом же цикле.
        statuses = {
            stored.status
            for stored in command_service.list_commands(session)
            if stored.kind != str(CommandKind.NOTIFY)
        }
        assert statuses == {CommandStatus.SUCCESS}
        notify_jobs = command_service.list_commands(session, kind="notify")
        assert len(notify_jobs) == 1, "после полного обновления создано уведомление"
        assert notify_jobs[0].status == str(CommandStatus.QUEUED)

    async def test_send_message_reports_killed_process(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        seed_settings(session)
        store = make_store(session, online=True)
        command = add_command(
            session, store, CommandKind.SEND_MESSAGE, {"text_message": "База будет обновлена"}
        )
        agent = ScriptedAgent()
        dispatcher = build_dispatcher(manager, settings, transport=agent.transport)

        await dispatcher.process_once()

        state = fresh(session, command.id)
        assert state.status == CommandStatus.SUCCESS
        messages = [
            entry.message
            for entry in session.scalars(select(JobLog).where(JobLog.command_id == command.id))
        ]
        assert any("1cv8.exe завершён: да" in message for message in messages)


class TestUnfinishedOperation:
    """Наблюдение №7: «202» и пустой лог не должны выглядеть как «идёт» вечно."""

    async def test_operation_that_never_started_is_failed(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        seed_settings(session)
        store = make_store(session, online=True)
        command = add_command(session, store, CommandKind.RUN_MERGE, {"file_name": "update"})
        agent = ScriptedAgent(checks_before_success=None, busy=False)
        dispatcher = build_dispatcher(manager, settings, transport=agent.transport)

        await dispatcher.process_once()  # 202 на /run-merge
        await dispatcher.process_once()  # 202 на /check-merge, касса не занята

        state = fresh(session, command.id)
        assert state.status == CommandStatus.FAILED
        assert "запуск не состоялся" in (state.last_error or "")

    async def test_interrupted_operation_is_reported_to_admin(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        seed_settings(session)
        store = make_store(session, online=True)
        command = add_command(
            session, store, CommandKind.RUN_UPDATE_DB, {"is_fast_update": False}
        )
        agent = ScriptedAgent(
            checks_before_success=None, busy=False, interrupted="update-db (лог log-update-db.txt)"
        )
        dispatcher = build_dispatcher(manager, settings, transport=agent.transport)

        await dispatcher.process_once()
        await dispatcher.process_once()

        state = fresh(session, command.id)
        assert state.status == CommandStatus.FAILED
        assert "прервана" in (state.last_error or "")

    async def test_long_operation_hits_poll_timeout(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        seed_settings(session, command_poll_timeout_sec="0")
        store = make_store(session, online=True)
        command = add_command(session, store, CommandKind.RUN_MERGE, {"file_name": "update"})
        agent = ScriptedAgent(checks_before_success=None)
        dispatcher = build_dispatcher(manager, settings, transport=agent.transport)

        await dispatcher.process_once()  # отправка, started_at заполнен

        # Отсчёт таймаута сдвигаем в прошлое: с нулевым пределом проверка иначе
        # зависела бы от часов — два цикла успевают пройти за доли миллисекунды.
        state = fresh(session, command.id)
        state.started_at = utcnow() - timedelta(seconds=5)
        session.commit()

        await dispatcher.process_once()  # предел ожидания вышел

        state = fresh(session, command.id)
        assert state.status == CommandStatus.TIMEOUT
        assert "предел ожидания" in (state.last_error or "")


class TestNoDuplicateOnBrokenChannel:
    """Вариант B (Д-15): разрыв при отправке `/run-*` не должен давать дубль.

    Когда ответ агента на `/run-merge`/`/run-update-db` не дошёл (касса «в сети»),
    сервер не знает, принял ли агент команду. Переотправка на свободную кассу
    привела бы к двойному выполнению операции. Вместо этого задача переводится
    в `SENT` и допрашивается через `/check-*`: если агент выполнил — успех;
    если агент не знает об операции — возврат в `queued` для честной повторной
    отправки.
    """

    def _broken_send_transport(
        self, *, agent_executed: bool, requests: list[dict[str, object]]
    ) -> httpx.MockTransport:
        """Транспорт: `/run-*` рвётся, `/check-*` знает результат операции."""
        busy = {"merge": False, "update": False}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            requests.append({"method": request.method, "path": path})
            if path == "/health":
                return httpx.Response(200, json=health_body(busy=busy))
            if path == "/run-merge":
                raise httpx.ConnectError(
                    "соединение разорвано: удалённый хост не ответил", request=request
                )
            if path == "/check-merge":
                if agent_executed:
                    return httpx.Response(
                        200,
                        json={
                            "status": "success",
                            "message": "Merge completed successfully.",
                            "logs": "Объединение конфигураций успешно завершено",
                        },
                    )
                return httpx.Response(
                    202,
                    json={
                        "status": "not_found",
                        "message": "Operation is in progress.",
                        "logs": "",
                    },
                )
            return httpx.Response(404, json={"detail": f"Неизвестный путь {path}"})

        return transport_from_handler(handler)

    async def test_agent_executed_but_response_lost_gets_polled_not_resent(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        """Агент выполнил merge, но ответ `/run-merge` не дошёл — допрос, не дубль."""
        seed_settings(session)
        store = make_store(session, online=True)
        command = add_command(session, store, CommandKind.RUN_MERGE, {"file_name": "update"})
        requests: list[dict[str, object]] = []
        dispatcher = build_dispatcher(
            manager,
            settings,
            transport=self._broken_send_transport(agent_executed=True, requests=requests),
        )

        first = await dispatcher.process_once()  # разрыв на отправке
        state = fresh(session, command.id)
        assert state.status == CommandStatus.SENT, (
            "задача переходит в SENT и допрашивается, а не возвращается в queued"
        )
        assert state.attempts == 0, "неудачная отправка не «тратит» попытку"
        assert first.retried == 0, "это не retry, а досыл опроса"
        assert first.waiting == 1
        assert [r["path"] for r in requests].count("/run-merge") == 1

        await dispatcher.process_once()  # /check-merge → 200: агент уже выполнил

        state = fresh(session, command.id)
        assert state.status == CommandStatus.SUCCESS, "опрос подтверждает успех"
        assert "/check-merge" in [r["path"] for r in requests]
        assert (
            [r["path"] for r in requests].count("/run-merge") == 1
        ), "операция не отправляется повторно — дубля merge нет"

    async def test_agent_never_received_goes_back_to_queue_and_resent(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        """Разрыв до доставки: `/check-*` не знает операцию → возврат в очередь."""
        seed_settings(session)
        store = make_store(session, online=True)
        command = add_command(session, store, CommandKind.RUN_MERGE, {"file_name": "update"})
        requests: list[dict[str, object]] = []
        dispatcher = build_dispatcher(
            manager,
            settings,
            transport=self._broken_send_transport(agent_executed=False, requests=requests),
        )

        await dispatcher.process_once()  # разрыв на отправке → SENT (провизорный)
        assert fresh(session, command.id).status == CommandStatus.SENT

        await dispatcher.process_once()  # /check-merge → 202 not_found, касса свободна
        assert fresh(session, command.id).status == CommandStatus.QUEUED, (
            "агент не знает операцию — задача возвращается в очередь"
        )

        await dispatcher.process_once()  # повторная отправка снова рвётся
        state = fresh(session, command.id)
        assert state.status == CommandStatus.SENT
        # Честная повторная отправка выполняется заново, а не «висит» в опросе
        assert state.attempts == 0
        assert [r["path"] for r in requests].count("/run-merge") == 2


class TestNotifyAfterFullUpdate:
    """Д-18: после успешного **полного** обновления БД кассирам ставится уведомление
    «Можно входить в программу 1С» (важно, без подтверждения, удаляется после
    прочтения, без категории)."""

    async def test_full_update_creates_can_login_notification(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        seed_settings(session)
        store = make_store(session, online=True)
        command = add_command(
            session, store, CommandKind.RUN_UPDATE_DB, {"is_fast_update": False}
        )
        agent = ScriptedAgent()
        dispatcher = build_dispatcher(manager, settings, transport=agent.transport)

        await dispatcher.process_once()  # отправка
        await dispatcher.process_once()  # /check-update-db → 200 success

        assert fresh(session, command.id).status == CommandStatus.SUCCESS

        notifications = list(session.scalars(select(Notification)))
        assert len(notifications) == 1, "после полного обновления создано уведомление"
        assert notifications[0].title == "Можно входить в программу 1С"
        assert notifications[0].severity == "important"
        assert notifications[0].requires_ack is False
        assert notifications[0].delete_after_read is True

        notify_jobs = list(
            session.scalars(select(Command).where(Command.kind == str(CommandKind.NOTIFY)))
        )
        assert len(notify_jobs) == 1, "поставлена notify-задача на кассу"
        assert notify_jobs[0].params == {"notification_id": notifications[0].id}

    async def test_fast_update_does_not_notify(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        seed_settings(session)
        store = make_store(session, online=True)
        command = add_command(
            session, store, CommandKind.RUN_UPDATE_DB, {"is_fast_update": True}
        )
        agent = ScriptedAgent()
        dispatcher = build_dispatcher(manager, settings, transport=agent.transport)

        await dispatcher.process_once()
        await dispatcher.process_once()

        assert fresh(session, command.id).status == CommandStatus.SUCCESS
        notifications = list(session.scalars(select(Notification)))
        assert notifications == [], "быстрое обновление — без уведомления «Можно входить»"


class TestCredentialsPush:
    """П-1/§8.3: сервер сам досылает учётные данные 1С по расхождению версий."""

    async def test_password_is_pushed_when_versions_differ(
        self,
        manager: DatabaseManager,
        settings: Settings,
        session: Session,
        secret_box: SecretBox,
    ) -> None:
        seed_settings(session)
        store = make_store(
            session,
            online=True,
            password=STORE_PASSWORD,
            credentials_version=4,
            secret_box=secret_box,
        )
        agent = ScriptedAgent(credentials_version=1)
        dispatcher = build_dispatcher(
            manager, settings, transport=agent.transport, secret_box=secret_box
        )

        report = await dispatcher.process_once()

        assert report.credentials_pushed == 1
        assert agent.credentials_pushes[0]["credentials_version"] == 4
        assert agent.credentials_pushes[0]["password"] == STORE_PASSWORD
        assert agent.credentials_version == 4, "касса приняла новую версию"

        again = await dispatcher.process_once()
        assert again.credentials_pushed == 0
        assert len(agent.credentials_pushes) == 1, "сверка не чаще credentials_push_interval_sec"

    async def test_nothing_is_pushed_when_versions_match(
        self,
        manager: DatabaseManager,
        settings: Settings,
        session: Session,
        secret_box: SecretBox,
    ) -> None:
        seed_settings(session)
        make_store(
            session,
            online=True,
            password=STORE_PASSWORD,
            credentials_version=4,
            secret_box=secret_box,
        )
        agent = ScriptedAgent(credentials_version=4)
        dispatcher = build_dispatcher(
            manager, settings, transport=agent.transport, secret_box=secret_box
        )

        report = await dispatcher.process_once()

        assert report.credentials_pushed == 0
        assert agent.credentials_pushes == []

    async def test_password_is_resent_before_merge_when_versions_differ(
        self,
        manager: DatabaseManager,
        settings: Settings,
        session: Session,
        secret_box: SecretBox,
    ) -> None:
        """П-1: перед `/run-merge` сервер досылает пароль, если версии расходятся.

        Если касса ещё не получила текущую версию пароля (например, была офлайн
        и push не дошёл), запуск слияния ушёл бы без `/P` — 1С открыла бы диалог
        аутентификации и конфигуратор завис. Гарантия: push до постановки операции.
        """
        seed_settings(session)
        store = make_store(
            session,
            online=True,
            password=STORE_PASSWORD,
            credentials_version=4,
            secret_box=secret_box,
        )
        command = add_command(
            session, store, CommandKind.RUN_MERGE, {"file_name": "update"}
        )
        agent = ScriptedAgent(credentials_version=1)
        dispatcher = build_dispatcher(
            manager, settings, transport=agent.transport, secret_box=secret_box
        )

        report = await dispatcher.process_once()

        paths = agent.paths()
        merge_at = paths.index("/run-merge")
        assert "/credentials" in paths[:merge_at], (
            "пароль досылается до запуска слияния, а не после него"
        )
        assert agent.credentials_version == 4, "касса приняла актуальную версию пароля"
        assert fresh(session, command.id).status == CommandStatus.SENT
        assert report.credentials_pushed >= 1


class TestDevicesPush:
    """Д-1/§7.6: сервер сам досылает список оборудования по расхождению версий."""

    async def test_list_is_pushed_when_versions_differ(
        self,
        manager: DatabaseManager,
        settings: Settings,
        session: Session,
        secret_box: SecretBox,
    ) -> None:
        seed_settings(session)
        store = make_store(session, online=True)
        device_service.create_device(session, store, name="камера вход", ip_address="10.10.1.21")
        device_service.create_device(
            session,
            store,
            name="камера двор",
            ip_address="10.10.1.40",
            check_type="tcp",
            check_port=554,
        )
        disabled = device_service.create_device(
            session, store, name="отключённая", ip_address="10.10.1.99"
        )
        device_service.deactivate_device(session, store, disabled)
        session.commit()
        agent = ScriptedAgent(devices_version=1, credentials_version=store.credentials_version)
        dispatcher = build_dispatcher(
            manager, settings, transport=agent.transport, secret_box=secret_box
        )

        report = await dispatcher.process_once()

        assert report.devices_pushed == 1
        pushed = agent.devices_pushes[0]
        assert pushed["devices_version"] == store.devices_version
        assert [item["ip"] for item in pushed["devices"]] == ["10.10.1.21", "10.10.1.40"], (
            "отключённое устройство агенту не нужно (§11.7)"
        )
        assert pushed["devices"][1]["check_type"] == "tcp"
        assert pushed["devices"][1]["port"] == 554
        assert pushed["check_interval_sec"] == 60, "расписание — из настроек §4.9 (Д-9)"
        assert pushed["ping_timeout_ms"] == 1000
        assert agent.devices_version == store.devices_version, "касса приняла новую версию"
        assert agent.paths().count("/health") == 1, "один /health отвечает про П-1 и Д-1"

        again = await dispatcher.process_once()
        assert again.devices_pushed == 0
        assert len(agent.devices_pushes) == 1, "сверка не чаще credentials_push_interval_sec"

    async def test_nothing_is_pushed_when_versions_match(
        self,
        manager: DatabaseManager,
        settings: Settings,
        session: Session,
    ) -> None:
        seed_settings(session)
        store = make_store(session, online=True, devices_version=3)
        agent = ScriptedAgent(devices_version=3)
        dispatcher = build_dispatcher(manager, settings, transport=agent.transport)

        report = await dispatcher.process_once()

        assert report.devices_pushed == 0
        assert agent.devices_pushes == []
        assert store.devices_version == 3, "версию на сервере push не меняет"

    async def test_offline_store_is_not_pushed(
        self,
        manager: DatabaseManager,
        settings: Settings,
        session: Session,
    ) -> None:
        """Офлайн-касса — не ошибка: список уедет, когда магазин появится (§3.2)."""
        seed_settings(session)
        store = make_store(session, online=False, devices_version=2)
        device_service.create_device(session, store, name="свитч", ip_address="10.10.1.30")
        session.commit()
        agent = ScriptedAgent(devices_version=1)
        dispatcher = build_dispatcher(manager, settings, transport=agent.transport)

        report = await dispatcher.process_once()

        assert report.devices_pushed == 0
        assert agent.devices_pushes == []



class TestDownloadScenario:
    """ФН2: «разослать ссылку» → дождаться файла → повторная проверка (§11.3)."""

    async def test_download_waits_for_file_and_orders_check(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        seed_settings(session)
        store = make_store(session, online=True)
        command = add_command(
            session,
            store,
            CommandKind.DOWNLOAD_FILE,
            {"url-file": "https://disk.yandex.ru/d/XXXX/update.cf"},
        )
        agent = ScriptedAgent(download_polls_before_file=2)
        dispatcher = build_dispatcher(manager, settings, transport=agent.transport)

        await dispatcher.process_once()
        state = fresh(session, command.id)
        assert state.status == CommandStatus.RUNNING
        assert state.log_files == ["update"], "имя файла из ссылки (basename)"

        await dispatcher.process_once()
        assert fresh(session, command.id).status == CommandStatus.RUNNING, "файла ещё нет"

        await dispatcher.process_once()
        assert fresh(session, command.id).status == CommandStatus.SUCCESS

        follow_ups = command_service.list_commands(session, kind="check_files")
        assert follow_ups, "после скачивания сервер сам заказывает проверку (§11.3)"

        await dispatcher.process_once()
        snapshots = list(session.scalars(select(FileCheck)))
        assert snapshots and snapshots[-1].cf_exists

    async def test_download_uses_real_filename_from_agent(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        """Ссылка без имени файла: сервер узнаёт имя из `downloaded` (§11.3, FR-1).

        Для `https://disk.yandex.ru/d/<id>` basename — это id ресурса, а не имя
        пакета. Агент сообщает реальное имя скачанного файла, сервер
        переспрашивает `/check-files` по нему и завершает задачу.
        """
        seed_settings(session)
        store = make_store(session, online=True)
        command = add_command(
            session,
            store,
            CommandKind.DOWNLOAD_FILE,
            {"url-file": "https://disk.yandex.ru/d/ukPxhdZu6Ax4WQ"},
        )
        agent = ScriptedAgent(downloaded_file_name="update.cf")
        dispatcher = build_dispatcher(manager, settings, transport=agent.transport)

        # 1-й цикл: сервер отправил /download-file. 2-й: /check-files по имени из
        # ссылки не находит файл, но агент сообщает `downloaded` — сервер
        # переспрашивает по реальному имени и завершает задачу.
        await dispatcher.process_once()
        await dispatcher.process_once()

        state = fresh(session, command.id)
        assert state.status == CommandStatus.SUCCESS
        assert state.log_files == ["ukPxhdZu6Ax4WQ"], "log_files — имя из ссылки (basename)"

        follow_ups = command_service.list_commands(session, kind="check_files")
        assert follow_ups, "после скачивания сервер сам заказывает проверку (§11.3)"
        assert follow_ups[0].params == {"filename": "update"}, "переспрос по реальному имени"

        await dispatcher.process_once()
        snapshots = list(session.scalars(select(FileCheck)))
        assert snapshots and snapshots[-1].cf_exists


    async def test_download_that_never_appears_hits_timeout(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        seed_settings(session, command_poll_timeout_sec="0")
        store = make_store(session, online=True)
        command = add_command(
            session, store, CommandKind.DOWNLOAD_FILE, {"url-file": "https://disk.yandex.ru/d/X/update.cf"}
        )
        agent = ScriptedAgent(download_polls_before_file=99)
        dispatcher = build_dispatcher(manager, settings, transport=agent.transport)

        await dispatcher.process_once()
        await dispatcher.process_once()

        assert fresh(session, command.id).status == CommandStatus.TIMEOUT


    async def test_download_failed_status_completes_failed(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        """Скачивание провалилось: агент сообщает `failed`, задача закрывается.

        Урок Э3 (наблюдение №7): нельзя ждать файл до таймаута, если касса уже
        знает, что скачивание не удалось (сетевой сбой, Диск не отдал ссылку).
        Агент пишет `status: failed` в `last-download.json` и сообщает его через
        `/check-files`; диспетчер завершает задачу `failed` с текстом ошибки.
        """
        seed_settings(session, command_poll_timeout_sec="3600")
        store = make_store(session, online=True)
        command = add_command(
            session,
            store,
            CommandKind.DOWNLOAD_FILE,
            {"url-file": "https://disk.yandex.ru/d/ukPxhdZu6Ax4WQ"},
        )
        agent = ScriptedAgent(
            download_failed=True,
            download_error="404 — ресурс не найден или ссылка недоступна",
        )
        dispatcher = build_dispatcher(manager, settings, transport=agent.transport)

        # 1-й цикл: /download-file. 2-й: /check-files сообщает failed → задача
        # закрывается, не дожидаясь файла.
        await dispatcher.process_once()
        await dispatcher.process_once()

        state = fresh(session, command.id)
        assert state.status == CommandStatus.FAILED
        assert state.last_error and "404" in state.last_error, "текст ошибки уходит в задачу"
        assert state.log_files == [
            "ukPxhdZu6Ax4WQ"
        ], "log_files — имя из ссылки (basename), файл не появился"


class TestDispatcherLifecycle:
    """Запуск/остановка и FR-24: перезапуск сервера не теряет очередь."""

    async def test_start_is_idempotent_and_stop_works(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        seed_settings(session)
        dispatcher = build_dispatcher(
            manager, settings, transport=ScriptedAgent().transport
        )

        task = dispatcher.start()
        assert dispatcher.is_running
        assert dispatcher.start() is task, "повторный start не создаёт вторую задачу"
        await asyncio.sleep(0.05)
        await dispatcher.stop()

        assert not dispatcher.is_running
        assert dispatcher.interval_sec > 0

    async def test_pending_queue_is_picked_up_after_restart(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        seed_settings(session)
        store = make_store(session, online=True)
        command = add_command(session, store, CommandKind.CHECK_FILES, {"filename": "update"})

        agent = ScriptedAgent()
        restarted = build_dispatcher(manager, settings, transport=agent.transport)
        await restarted.process_once()

        assert "/check-files" in agent.paths()
        assert fresh(session, command.id).status == CommandStatus.SUCCESS


class TestPayloadHelpers:
    """Мелкие правила, которые легко нарушить: сборка тела и разбор имени файла."""

    def test_build_1c_payload_never_contains_password(self, session: Session) -> None:
        store = make_store(session, online=False)
        command = add_command(
            session,
            store,
            CommandKind.RUN_MERGE,
            {"file_name": "update", "full_merge": True, "password": "нельзя"},
        )

        payload = build_1c_payload(store, command)

        assert payload == {
            "server_name": "srv-1c",
            "base_name": "trade",
            "version_1c": "8.3.24.1548",
            "user_name": "Администратор",
            "file_name": "update",
            "full_merge": True,
            "is_extension": False,
        }

    def test_build_1c_payload_fills_extension_from_store(self, session: Session) -> None:
        """FR-4: имя расширения подставляется из карточки магазина, если не задано явно."""
        store = make_store(session, online=False, extension_name="БГ_Торговоя")
        command = add_command(
            session, store, CommandKind.RUN_MERGE, {"file_name": "update"}
        )

        payload = build_1c_payload(store, command)
        assert payload["extension_name"] == "БГ_Торговоя"

    def test_build_1c_payload_explicit_extension_wins(self, session: Session) -> None:
        """Явное имя расширения в параметрах операции приоритетнее карточки магазина."""
        store = make_store(session, online=False, extension_name="БГ_Торговоя")
        command = add_command(
            session,
            store,
            CommandKind.RUN_MERGE,
            {"file_name": "update", "extension_name": "ДругоеРасширение"},
        )

        payload = build_1c_payload(store, command)
        assert payload["extension_name"] == "ДругоеРасширение"

    def test_build_1c_payload_update_db_is_extension_controls_extension(
        self, session: Session
    ) -> None:
        """Д-17: явный `is_extension` для update-db главнее карточки магазина."""
        store = make_store(session, online=False, extension_name="БГ_Торговоя")

        main = add_command(
            session,
            store,
            CommandKind.RUN_UPDATE_DB,
            {"is_fast_update": False, "is_extension": False},
        )
        payload = build_1c_payload(store, main)
        assert "extension_name" not in payload, "основная конфигурация — без расширения"

        ext = add_command(
            session,
            store,
            CommandKind.RUN_UPDATE_DB,
            {"is_fast_update": False, "is_extension": True},
        )
        payload = build_1c_payload(store, ext)
        assert payload["extension_name"] == "БГ_Торговоя"

    def test_build_1c_payload_cf_file_ignores_store_extension(
        self, session: Session
    ) -> None:
        """Д-16: явный `.cf` в имени файла → основная конфигурация, без расширения.

        Иначе `.cf`-слияние на магазине с настроенным расширением было бы
        невозможно: карточка подменяла бы `.cf` на `.cfe`.
        """
        store = make_store(session, online=False, extension_name="БГ_Торговоя")
        command = add_command(
            session, store, CommandKind.RUN_MERGE, {"file_name": "1cv8.cf"}
        )

        payload = build_1c_payload(store, command)
        assert "extension_name" not in payload

    def test_build_1c_payload_cfe_file_keeps_store_extension(
        self, session: Session
    ) -> None:
        """Д-16: явный `.cfe` в имени файла → расширение; имя — из карточки."""
        store = make_store(session, online=False, extension_name="БГ_Торговоя")
        command = add_command(
            session, store, CommandKind.RUN_MERGE, {"file_name": "1cv8.cfe"}
        )

        payload = build_1c_payload(store, command)
        assert payload["extension_name"] == "БГ_Торговоя"

    @pytest.mark.parametrize(
        ("url", "name", "kind"),
        [
            ("https://disk.yandex.ru/d/XXXX/update.cf", "update", "cf"),
            ("https://disk.yandex.ru/d/XXXX/update.cfe", "update", "cfe"),
            ("https://disk.yandex.ru/d/XXXX/full_merge.xml", "full_merge", "xml"),
            ("https://disk.yandex.ru/d/XXXX/pkg.zip", "pkg.zip", None),
        ],
    )
    def test_extract_file_name_and_kind(self, url: str, name: str, kind: str | None) -> None:
        from server.core.dispatcher import expected_file_kind, extract_file_name

        assert extract_file_name(url) == name
        assert expected_file_kind(url) == kind

    def test_agent_protocol_error_carries_status(self) -> None:
        """`409` от агента опознаётся диспетчером по коду, а не по тексту (§7.3)."""
        error = AgentProtocolError("Агент ответил статусом 409", status_code=409)

        assert error.status_code == 409

    async def test_agent_409_keeps_command_in_queue(
        self, manager: DatabaseManager, settings: Settings, session: Session
    ) -> None:
        """I-13: на кассе уже идёт операция — команда ждёт, попытки не тратятся."""
        seed_settings(session)
        store = make_store(session, online=True)
        command = add_command(session, store, CommandKind.RUN_MERGE, {"file_name": "update"})
        dispatcher = build_dispatcher(
            manager, settings, transport=status_transport(409), backoff_base_sec=0.0
        )

        report = await dispatcher.process_once()

        state = fresh(session, command.id)
        assert state.status == CommandStatus.QUEUED, "команда не теряется"
        assert state.attempts == 0, "409 — не повод сжигать попытки"
        assert report.waiting == 1
