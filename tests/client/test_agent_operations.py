"""Мьютекс, состояние операций и Windows-утилиты агента (§7.3, FR-1, FR-8, I-13).

Здесь проверки, которых не было в v2: `409 Conflict` при параллельном запуске
(Q23, FR-22), `agent-state.json` и пометка `interrupted` после перезагрузки
кассы (§7.3), подстановка пароля из профиля (П-1, §5.1.1 п. 2) и разбор ФН2
(FR-1). Реальная 1С, `msg.exe` и `taskkill` не запускаются никогда (§7.5, §13).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from client.agent import credentials, downloader, messenger
from client.agent.app import create_agent_app
from client.agent.config import AGENT_STATE_FILE_NAME, OneCProfile
from client.agent.executor import (
    OPERATION_MERGE,
    STATE_FINISHED,
    STATE_INTERRUPTED,
    OperationBusyError,
    OperationRunner,
)
from client.agent.one_c import SUCCESS_MERGE_MSG
from shared.schemas import BusyState, MergeParams, UpdateParams
from tests.client.conftest import RecordingProvider
from tests.client.test_agent_one_c_api import (
    HEADERS,
    MERGE_BODY,
    UPDATE_BODY,
    agent_api,
    wait_until,
)


class TestOperationMutex:
    """I-13, Q23: `merge` и `update-db` не идут параллельно на одной базе."""

    async def test_second_merge_while_first_runs_is_409(self, one_c_config) -> None:  # noqa: ANN001
        provider = RecordingProvider(blocked=True)

        async with agent_api(one_c_config, provider) as (http, app):
            first = await http.post("/run-merge", json=MERGE_BODY, headers=HEADERS)
            await wait_until(lambda: provider.commands)

            second = await http.post("/run-merge", json=MERGE_BODY, headers=HEADERS)
            health = await http.get("/health", headers=HEADERS)

            provider.release()
            await app.state.agent_runner.wait_idle()

        assert first.status_code == 202
        assert second.status_code == 409
        assert "уже" in second.json()["detail"]
        assert health.json()["busy"] == {"merge": True, "update": False}
        assert len(provider.commands) == 1, "вторая операция до 1С не дошла"

    async def test_update_during_merge_is_409(self, one_c_config) -> None:  # noqa: ANN001
        """Приёмка Приложения A: параллельные merge и update → второй получает `409`."""
        provider = RecordingProvider(blocked=True)

        async with agent_api(one_c_config, provider) as (http, app):
            await http.post("/run-merge", json=MERGE_BODY, headers=HEADERS)
            await wait_until(lambda: provider.commands)
            blocked = await http.post("/run-update-db", json=UPDATE_BODY, headers=HEADERS)

            provider.release()
            await app.state.agent_runner.wait_idle()

        assert blocked.status_code == 409

    async def test_agent_is_free_after_operation_finishes(self, one_c_config) -> None:  # noqa: ANN001
        provider = RecordingProvider(success_message=SUCCESS_MERGE_MSG)

        async with agent_api(one_c_config, provider) as (http, app):
            await http.post("/run-merge", json=MERGE_BODY, headers=HEADERS)
            await app.state.agent_runner.wait_idle()

            health = await http.get("/health", headers=HEADERS)
            again = await http.post("/run-merge", json=MERGE_BODY, headers=HEADERS)
            await app.state.agent_runner.wait_idle()

        assert health.json()["busy"] == {"merge": False, "update": False}
        assert again.status_code == 202, "после завершения операции агент снова свободен"


class TestOperationStateFile:
    """§7.3: `agent-state.json` — что делал агент и что прервала перезагрузка."""

    async def test_finished_operation_is_written(self, one_c_config, work_dir: Path) -> None:  # noqa: ANN001
        provider = RecordingProvider(success_message=SUCCESS_MERGE_MSG)

        async with agent_api(one_c_config, provider) as (http, app):
            await http.post("/run-merge", json=MERGE_BODY, headers=HEADERS)
            await app.state.agent_runner.wait_idle()

        state = json.loads((work_dir / AGENT_STATE_FILE_NAME).read_text(encoding="utf-8"))
        assert state["operation"] == OPERATION_MERGE
        assert state["status"] == STATE_FINISHED
        assert state["file_name"] == "1cv8"
        assert state["log_file"].endswith("log-merge.txt")
        assert state["finished_at"].endswith("Z")

    def test_interrupted_operation_is_reported_in_health(
        self, one_c_config, work_dir: Path
    ) -> None:  # noqa: ANN001
        """Касса перезагрузилась посреди обновления — админ это видит (§7.3, FR-22)."""
        (work_dir / AGENT_STATE_FILE_NAME).write_text(
            json.dumps(
                {
                    "operation": "update-db",
                    "status": "running",
                    "log_file": str(work_dir / "log-update-db.txt"),
                    "started_at": "2026-10-13T10:00:00Z",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with TestClient(create_agent_app(one_c_config, provider=RecordingProvider())) as client:
            health = client.get("/health", headers=HEADERS).json()

        state = json.loads((work_dir / AGENT_STATE_FILE_NAME).read_text(encoding="utf-8"))
        assert health["interrupted_operation"] == "update-db"
        assert state["status"] == STATE_INTERRUPTED
        assert state["finished_at"].endswith("Z")

    async def test_new_operation_replaces_interrupted_report(
        self, one_c_config, work_dir: Path
    ) -> None:  # noqa: ANN001
        (work_dir / AGENT_STATE_FILE_NAME).write_text(
            json.dumps(
                {
                    "operation": "update-db",
                    "status": "running",
                    "log_file": "log-update-db.txt",
                    "started_at": "2026-10-13T10:00:00Z",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        provider = RecordingProvider(success_message=SUCCESS_MERGE_MSG)

        async with agent_api(one_c_config, provider) as (http, app):
            # В ASGI-тесте жизненного цикла нет — старт службы имитируем явно
            app.state.agent_runner.mark_interrupted()
            before = (await http.get("/health", headers=HEADERS)).json()

            await http.post("/run-merge", json=MERGE_BODY, headers=HEADERS)
            await app.state.agent_runner.wait_idle()
            after = (await http.get("/health", headers=HEADERS)).json()

        assert before["interrupted_operation"] == "update-db"
        assert after["interrupted_operation"] is None, "новая операция снимает прошлый отчёт"


class TestRunnerDirect:
    """Юнит-проверки исполнителя без HTTP: отказ и освобождение (§7.3)."""

    async def test_second_submit_raises_busy_error(self, one_c_paths) -> None:  # noqa: ANN001
        provider = RecordingProvider(blocked=True)
        runner = OperationRunner(
            paths=one_c_paths,
            state_path=one_c_paths.work_dir / AGENT_STATE_FILE_NAME,
            provider=provider,
        )
        await runner.submit_merge(MergeParams(**MERGE_BODY))
        await wait_until(lambda: provider.commands)

        with pytest.raises(OperationBusyError):
            await runner.submit_update_db(UpdateParams(**UPDATE_BODY))

        provider.release()
        await runner.wait_idle()
        await runner.shutdown()

        assert runner.busy_state == BusyState()
        assert runner.current_state is not None
        assert runner.current_state.status == STATE_FINISHED


class TestProfilePassword:
    """П-1, §5.1.1 п. 2: пароль может прийти из зашифрованного профиля агента."""

    def test_explicit_password_wins(self) -> None:
        profile = OneCProfile(user_name="Администратор", password_enc="токен")
        resolved = credentials.resolve_password(
            "ИзЗапроса", profile, decryptor=lambda token: "ИзПрофиля"
        )
        assert resolved == "ИзЗапроса"

    def test_profile_password_is_used_when_absent(self) -> None:
        profile = OneCProfile(user_name="Администратор", password_enc="токен")
        resolved = credentials.resolve_password("", profile, decryptor=lambda token: "ИзПрофиля")
        assert resolved == "ИзПрофиля"

    def test_encrypted_profile_without_decryptor_gives_empty(self) -> None:
        """Расшифровка (Fernet + DPAPI) подключается на Э4: без неё пароля нет."""
        profile = OneCProfile(user_name="Администратор", password_enc="токен")
        assert credentials.resolve_password("", profile) == ""

    def test_no_profile_is_empty_password_like_v2(self) -> None:
        assert credentials.resolve_password("", OneCProfile()) == ""

    def test_apply_profile_password_does_not_change_request(self) -> None:
        params = MergeParams(**{**MERGE_BODY, "password": ""})
        profile = OneCProfile(user_name="Администратор", password_enc="токен")

        prepared = credentials.apply_profile_password(
            params, profile, decryptor=lambda token: "Секрет"
        )

        assert prepared.password == "Секрет"
        assert params.password == "", "параметры запроса не меняем (I-7, без следов)"


class TestDownloaderYandexDisk:
    """FR-1, I-2: скачивание пакета и имя файла — только basename."""

    @staticmethod
    def _client(*, name: str = "update.zip", status: int = 200) -> httpx.AsyncClient:
        """httpx-клиент с подменённым API Яндекс.Диска (§13: сеть не нужна)."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "cloud-api.yandex.net":
                return httpx.Response(
                    200, json={"href": "https://down.example/file", "name": name}
                )
            return httpx.Response(status, content=b"package")

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_file_is_saved_by_basename(self, work_dir: Path) -> None:
        async with self._client() as client:
            path = await downloader.download_file_from_yandex_disk(
                "https://disk.yandex.ru/d/abc", work_dir=work_dir, client=client
            )

        assert path == work_dir / "update.zip"
        assert path is not None and path.read_bytes() == b"package"

    async def test_name_from_href_when_disk_omits_name(self, work_dir: Path) -> None:
        """Регресс «касса»: Яндекс.Диск не вернул поле `name`, но положил реальное
        имя файла в `href` (query-параметр `filename`). Имя обязано браться оттуда,
        иначе скачивание уходило в ошибку «ссылка не получена» (агент-лог).
        """

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "cloud-api.yandex.net":
                href = (
                    "https://downloader.disk.yandex.ru/disk/hash?uid=0"
                    "&filename=%D0%91%D0%93_%D0%A2%D0%BE%D1%80%D0%B3%D0%BE%D0%B2%D0%BE%D1%8F_mag-01072026.cfe"
                    "&disposition=attachment"
                )
                # name отсутствует — как в реальном ответе API для /d/<id>
                return httpx.Response(200, json={"method": "GET", "href": href})
            return httpx.Response(200, content=b"package")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            path = await downloader.download_file_from_yandex_disk(
                "https://disk.yandex.ru/d/ukPxhdZu6Ax4WQ", work_dir=work_dir, client=client
            )

        assert path == work_dir / "БГ_Торговоя_mag-01072026.cfe"
        assert path is not None and path.read_bytes() == b"package"

    async def test_unsafe_href_filename_is_sanitized_to_basename(self, work_dir: Path) -> None:
        """I-2: имя из `href` проходит через `safe_basename` — `..\\..\\evil.exe`
        (URL-закодированный) превращается в `evil.exe` и остаётся в рабочей папке,
        как и при имени из поля `name` (см. `test_traversal_name_stays_inside_work_dir`)."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "cloud-api.yandex.net":
                href = "https://downloader.disk.yandex.ru/disk/hash?filename=..%5C..%5Cevil.exe"
                return httpx.Response(200, json={"href": href})
            return httpx.Response(200, content=b"package")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            path = await downloader.download_file_from_yandex_disk(
                "https://disk.yandex.ru/d/abc", work_dir=work_dir, client=client
            )

        assert path == work_dir / "evil.exe"
        assert path is not None and path.parent == work_dir

    async def test_traversal_name_stays_inside_work_dir(self, work_dir: Path) -> None:
        """I-2: `..\\..\\evil.exe` из внешнего API не выходит за рабочую папку."""
        async with self._client(name="..\\..\\evil.exe") as client:
            path = await downloader.download_file_from_yandex_disk(
                "https://disk.yandex.ru/d/abc", work_dir=work_dir, client=client
            )

        assert path == work_dir / "evil.exe"
        assert path is not None and path.parent == work_dir

    async def test_unusable_name_returns_none(self, work_dir: Path) -> None:
        async with self._client(name="..") as client:
            path = await downloader.download_file_from_yandex_disk(
                "https://disk.yandex.ru/d/abc", work_dir=work_dir, client=client
            )

        assert path is None

    async def test_download_error_returns_none(self, work_dir: Path) -> None:
        """Ошибка диска — не исключение наружу: задача просто остаётся без файла."""
        async with self._client(status=500) as client:
            path = await downloader.download_file_from_yandex_disk(
                "https://disk.yandex.ru/d/abc", work_dir=work_dir, client=client
            )

        assert path is None

    async def test_own_client_ignores_env_proxy(self, work_dir: Path, monkeypatch) -> None:  # noqa: ANN001
        """Регресс «ловушки прокси»: когда скачиватель сам создаёт httpx-клиент,
        он обязан идти мимо системного прокси (туннеля) — `trust_env=False`.

        Без этого на кассе с прокси-туннелем реальное скачивание с Яндекс.Диска
        не доводится до конца (`download_status="failed"`), как было в `AgentClient`
        и `TrayAgentClient` (README, отклонение №2; см. `TestProxyIsIgnored`).
        """

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "cloud-api.yandex.net":
                return httpx.Response(
                    200, json={"href": "https://down.example/file", "name": "update.zip"}
                )
            return httpx.Response(200, content=b"package")

        captured: dict = {}
        real_client = httpx.AsyncClient

        def fake_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
            captured.update(kwargs)
            return real_client(transport=httpx.MockTransport(handler), *args, **kwargs)

        monkeypatch.setattr("client.agent.downloader.httpx.AsyncClient", fake_client)

        path = await downloader.download_file_from_yandex_disk(
            "https://disk.yandex.ru/d/abc", work_dir=work_dir
        )

        assert captured.get("trust_env") is False, "внутренний клиент не должен брать прокси из окружения"
        assert path == work_dir / "update.zip"


class TestMessenger:
    """FR-8: `msg */localhost` + `taskkill /f /im 1cv8.exe`, только argv (I-1)."""

    async def test_message_and_kill_are_argv_commands(self, fake_provider) -> None:  # noqa: ANN001
        killed = await messenger.send_windows_message("Обновление", provider=fake_provider)

        assert killed is True
        assert fake_provider.quiet_commands == [
            ["msg", "*/localhost", "Обновление"],
            ["taskkill", "/f", "/im", "1cv8.exe"],
        ]

    async def test_missing_msg_does_not_break_kill(self) -> None:
        """Урезанная Windows: `msg.exe` нет — 1С всё равно завершаем (FR-8)."""
        provider = RecordingProvider(quiet_available=False)

        killed = await messenger.send_windows_message("Обновление", provider=provider)

        assert killed is False
        assert provider.quiet_commands[-1] == ["taskkill", "/f", "/im", "1cv8.exe"]

