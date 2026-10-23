"""Эндпоинты 1С агента (§5.1.1) — перенос 15 тестов v2 через HTTP-слой (§9.1).

Проверяются коды ответов и тела эндпоинтов, обнуление лога **до** запуска (I-3),
фоновое выполнение операций (FR-9) и `409` при параллельном запуске (I-13).
Реальная 1С подменена провайдером `RecordingProvider` (§7.5), рабочая папка —
временная: на машине разработчика ничего не создаётся и не удаляется (§13).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from client.agent import downloader
from client.agent.app import create_agent_app
from client.agent.one_c import SUCCESS_MERGE_MSG, SUCCESS_UPDATE_MSG
from tests.client.conftest import AGENT_TOKEN, RecordingProvider

MERGE_BODY = {
    "server_name": "srv-1c",
    "base_name": "trade",
    "version_1c": "8.3.20.1234",
    "user_name": "Администратор",
    "password": "123",
    "file_name": "1cv8",
}

UPDATE_BODY = {
    "server_name": "srv-1c",
    "base_name": "trade",
    "version_1c": "8.3.20.1234",
    "user_name": "Администратор",
    "password": "123",
}

#: Заголовки сервера: токен магазина (§5.1)
HEADERS = {"X-Agent-Token": AGENT_TOKEN}


class NonZeroExitProvider(RecordingProvider):
    """1С `CONFIG /UpdateDBCfg` завершилась **ненулевым кодом** и не записала
    текст ни в `/Out`, ни в stdout — лог остаётся пустым (реальное поведение
    платформы при успешном обновлении, I-4: код возврата статус не решает)."""

    async def run_captured(
        self, args: list[str], *, log_path: Path | None = None, timeout: float | None = None
    ) -> int | None:
        self.commands.append(list(args))
        return 5


class BomOnlyExitProvider(RecordingProvider):
    """1С `CONFIG /UpdateDBCfg` завершилась и оставила в `/Out`-логе **только BOM**
    (U+FEFF) — реальное поведение платформы: файл создан с BOM-заголовком, но без
    текста. `str.strip()` BOM не удаляет, поэтому «пустота» должна учитывать его.
    """

    async def run_captured(
        self, args: list[str], *, log_path: Path | None = None, timeout: float | None = None
    ) -> int | None:
        self.commands.append(list(args))
        out_path = self._log_path_from(args)
        if out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("\ufeff", encoding="utf-8")
        return 5


async def wait_until(predicate: Callable[[], bool], *, timeout_sec: float = 2.0) -> None:
    """Дождаться условия: фоновая задача стартует не мгновенно (FR-9)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("условие не выполнено за отведённое время")
        await asyncio.sleep(0.01)


@asynccontextmanager
async def agent_api(  # noqa: ANN201 - контекстный менеджер теста
    config,
    provider: RecordingProvider,
    *,
    state_path: Path | None = None,
):  # noqa: ANN001
    """Поднять агент поверх ASGI с заданным провайдером 1С (§7.5, §13).

    Даёт пару «HTTP-клиент + приложение»: у приложения доступен
    `app.state.agent_runner`, поэтому тест может дождаться фоновой операции.
    """
    application = create_agent_app(config, provider=provider, state_path=state_path)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
        yield client, application


class TestFileSystemEndpoints:
    """`GET /` и `/check-files` — перенос v2 (FR-2) с расширением Q52."""

    async def test_status_endpoint_is_online(self, one_c_config) -> None:  # noqa: ANN001
        async with agent_api(one_c_config, RecordingProvider()) as (http, _):
            response = await http.get("/")

        assert response.status_code == 200
        assert response.json() == {"status": "online"}

    async def test_check_files(self, one_c_config, work_dir: Path) -> None:  # noqa: ANN001
        (work_dir / "1cv8.cf").write_text("cf", encoding="utf-8")
        (work_dir / "1cv8.xml").write_text("xml", encoding="utf-8")

        async with agent_api(one_c_config, RecordingProvider()) as (http, _):
            response = await http.post(
                "/check-files", json={"filename": "1cv8"}, headers=HEADERS
            )
        body = response.json()

        assert response.status_code == 200
        assert (body["cf_exists"], body["cfe_exists"], body["xml_exists"]) == (True, False, True)

    async def test_check_files_reports_sizes_and_dates(
        self, one_c_config, work_dir: Path
    ) -> None:  # noqa: ANN001
        """Q52: матрица файлов на дашборде показывает размер и дату."""
        (work_dir / "update.cf").write_bytes(b"12345")

        async with agent_api(one_c_config, RecordingProvider()) as (http, _):
            body = (
                await http.post("/check-files", json={"filename": "update"}, headers=HEADERS)
            ).json()

        assert body["cf_size"] == 5
        assert body["cf_mtime"].endswith("Z"), "даты в API — UTC (§6)"
        assert body["cfe_size"] is None and body["xml_mtime"] is None

    async def test_check_files_reports_last_download_name(
        self, one_c_config, work_dir: Path
    ) -> None:  # noqa: ANN001
        """FR-1: `/check-files` сообщает реальное имя скачанного файла (`downloaded`).

        Для ссылки Яндекс.Диска без имени файла сервер по этому полю узнаёт
        basename пакета и переспрашивает `/check-files` уже по нему.
        """
        (work_dir / "update.cf").write_text("cf", encoding="utf-8")
        (work_dir / downloader.LAST_DOWNLOAD_FILE).write_text(
            json.dumps({"status": "done", "filename": "update.cf"}),
            encoding="utf-8",
        )

        async with agent_api(one_c_config, RecordingProvider()) as (http, _):
            body = (
                await http.post(
                    "/check-files", json={"filename": "ukPxhdZu6Ax4WQ"}, headers=HEADERS
                )
            ).json()

        assert body["downloaded"] == "update.cf"
        assert body["cf_exists"] is False, "имя из ссылки не совпадает с реальным файлом"

    async def test_check_files_reports_download_failure(
        self, one_c_config, work_dir: Path
    ) -> None:  # noqa: ANN001
        """Провал скачивания: `/check-files` сообщает статус и текст ошибки.

        Агент пишет `status: failed` + `error` в `last-download.json`, когда Диск
        не отдал ссылку или произошёл сетевой сбой. Сервер по этим полям
        закрывает задачу `download_file` как `failed`, а не ждёт файл до таймаута.
        """
        (work_dir / downloader.LAST_DOWNLOAD_FILE).write_text(
            json.dumps(
                {
                    "status": "failed",
                    "filename": None,
                    "error": "404 — ресурс не найден или ссылка недоступна",
                }
            ),
            encoding="utf-8",
        )

        async with agent_api(one_c_config, RecordingProvider()) as (http, _):
            body = (
                await http.post(
                    "/check-files", json={"filename": "ukPxhdZu6Ax4WQ"}, headers=HEADERS
                )
            ).json()

        assert body["download_status"] == "failed"
        assert body["download_error"] == "404 — ресурс не найден или ссылка недоступна"
        assert body["downloaded"] is None, "имя при провале не отдаётся"
        assert body["cf_exists"] is False

    async def test_check_files_rejects_path_traversal(self, one_c_config) -> None:  # noqa: ANN001
        async with agent_api(one_c_config, RecordingProvider()) as (http, _):
            response = await http.post(
                "/check-files", json={"filename": "../../secret"}, headers=HEADERS
            )

        assert response.status_code == 422

    async def test_check_files_requires_token(self, one_c_client) -> None:  # noqa: ANN001
        assert one_c_client.post("/check-files", json={"filename": "update"}).status_code == 401


class TestMergeEndpoints:
    """`/run-merge` и `/check-merge` — перенос v2 (FR-3, FR-4, I-3)."""

    async def test_run_merge_clears_log_and_starts_operation(
        self, one_c_config, work_dir: Path
    ) -> None:  # noqa: ANN001
        """I-3: лог обнулён уже к ответу `202` — «залипший успех» невозможен."""
        log_path = work_dir / "log-merge.txt"
        log_path.write_text(SUCCESS_MERGE_MSG, encoding="utf-8")
        provider = RecordingProvider(blocked=True, success_message=SUCCESS_MERGE_MSG)

        async with agent_api(one_c_config, provider) as (http, app):
            response = await http.post("/run-merge", json=MERGE_BODY, headers=HEADERS)

            assert response.status_code == 202
            assert response.json()["log_path"] == str(log_path)
            assert log_path.read_text(encoding="utf-8") == "", "лог обнуляется до запуска"

            await wait_until(lambda: provider.commands)
            provider.release()
            await app.state.agent_runner.wait_idle()

        command = provider.commands[0]
        assert "/MergeCfg" in command and "-force" in command
        assert log_path.read_text(encoding="utf-8") == SUCCESS_MERGE_MSG

    async def test_run_merge_for_extension_uses_extension_log(
        self, one_c_config, work_dir: Path
    ) -> None:  # noqa: ANN001
        provider = RecordingProvider(blocked=True)

        async with agent_api(one_c_config, provider) as (http, app):
            response = await http.post(
                "/run-merge",
                json={**MERGE_BODY, "extension_name": "МоеРасширение"},
                headers=HEADERS,
            )
            await wait_until(lambda: provider.commands)
            provider.release()
            await app.state.agent_runner.wait_idle()

        assert response.json()["log_path"] == str(work_dir / "log-merge-extension.txt")
        command = provider.commands[0]
        assert command[command.index("/MergeCfg") + 1].endswith("1cv8.cfe")
        assert command.index("-Extension") < command.index("-Settings"), (
            "FR-4: порядок `/MergeCfg <файл> -Extension <имя> -force -Settings`"
        )
        assert command[command.index("-Settings") + 1].endswith("full_merge.xml"), (
            "FR-4: для расширения по умолчанию full_merge.xml"
        )

    async def test_check_merge_states(self, one_c_config, work_dir: Path) -> None:  # noqa: ANN001
        """Переходы статусов слияния (перенос v2): 202 → 400 → 200."""
        log_path = work_dir / "log-merge.txt"

        async with agent_api(one_c_config, RecordingProvider()) as (http, _):
            missing = await http.get("/check-merge", headers=HEADERS)
            assert missing.status_code == 202
            assert missing.json()["status"] == "not_found"

            log_path.write_text("", encoding="utf-8")
            in_progress = await http.get("/check-merge", headers=HEADERS)
            assert in_progress.status_code == 202
            assert in_progress.json()["status"] == "in_progress"

            log_path.write_text("Ошибка: файл не найден", encoding="utf-8")
            failed = await http.get("/check-merge", headers=HEADERS)
            assert failed.status_code == 400
            assert failed.json()["status"] == "failed"
            assert "Ошибка" in failed.json()["logs"]

            log_path.write_text(SUCCESS_MERGE_MSG, encoding="utf-8")
            success = await http.get("/check-merge", headers=HEADERS)
            assert success.status_code == 200
            assert success.json()["status"] == "success"

    async def test_check_merge_extension_flag(self, one_c_config, work_dir: Path) -> None:  # noqa: ANN001
        (work_dir / "log-merge-extension.txt").write_text(SUCCESS_MERGE_MSG, encoding="utf-8")

        async with agent_api(one_c_config, RecordingProvider()) as (http, _):
            response = await http.get(
                "/check-merge", params={"extension": "true"}, headers=HEADERS
            )

        assert response.status_code == 200
        assert response.json()["logs"] == SUCCESS_MERGE_MSG

    async def test_run_merge_rejects_path_traversal_file_name(self, one_c_client) -> None:  # noqa: ANN001
        response = one_c_client.post(
            "/run-merge", json={**MERGE_BODY, "file_name": "..\\..\\secret"}, headers=HEADERS
        )
        assert response.status_code == 422

    async def test_run_merge_requires_token(self, one_c_client) -> None:  # noqa: ANN001
        """§5.1: операции 1С выполняет только сервер со своим токеном."""
        assert one_c_client.post("/run-merge", json=MERGE_BODY).status_code == 401


class TestUpdateDbEndpoints:
    """`/run-update-db` и `/check-update-db` — перенос v2 (FR-5, FR-6)."""

    async def test_run_update_db_clears_log_and_does_full_cycle(
        self, one_c_config, work_dir: Path
    ) -> None:  # noqa: ANN001
        """`is_fast_update=false`: завершение сеансов (1С, без taskkill) → `/UpdateDBCfg` → разрешение (Д-18)."""
        log_path = work_dir / "log-update-db.txt"
        log_path.write_text(SUCCESS_UPDATE_MSG, encoding="utf-8")
        provider = RecordingProvider(blocked=True, success_message=SUCCESS_UPDATE_MSG)

        async with agent_api(one_c_config, provider) as (http, app):
            response = await http.post("/run-update-db", json=UPDATE_BODY, headers=HEADERS)

            assert response.status_code == 202
            assert response.json()["log_path"] == str(log_path)
            assert log_path.read_text(encoding="utf-8") == ""

            provider.release()
            await app.state.agent_runner.wait_idle()

        assert '/CЗавершитьРаботуПользователей,Сообщение="Обновляем конфигурацию"' in provider.commands[0]
        assert any("/UpdateDBCfg" in command for command in provider.commands)
        assert "/CРазрешитьРаботуПользователей" in provider.commands[-1]
        assert not any("taskkill" in command for command in provider.quiet_commands), (
            "Д-18: сеансы закрывает 1С, без принудительного taskkill"
        )
        assert log_path.read_text(encoding="utf-8") == SUCCESS_UPDATE_MSG

    async def test_run_update_db_empty_log_is_marked_success(
        self, one_c_config, work_dir: Path
    ) -> None:  # noqa: ANN001
        """FR-5: `/UpdateDBCfg` завершился кодом 0, но 1С не записала текст в `/Out`.

        Если оставить лог пустым, `/check-update-db` вечно отвечает `202`, и задача
        «висит» в работе, хотя обновление прошло. Исполнитель должен сам зафиксировать
        успех в логе, когда процесс вышел с кодом 0.
        """
        log_path = work_dir / "log-update-db.txt"
        provider = RecordingProvider()  # без success_message: 1С «не пишет» в /Out

        async with agent_api(one_c_config, provider) as (http, app):
            response = await http.post(
                "/run-update-db",
                json={**UPDATE_BODY, "is_fast_update": True},
                headers=HEADERS,
            )

            assert response.status_code == 202
            assert log_path.read_text(encoding="utf-8") == ""

            await app.state.agent_runner.wait_idle()

        assert any("/UpdateDBCfg" in command for command in provider.commands)
        assert log_path.read_text(encoding="utf-8") == SUCCESS_UPDATE_MSG

    async def test_run_update_db_nonzero_exit_empty_log_is_marked_success(
        self, one_c_config, work_dir: Path
    ) -> None:  # noqa: ANN001
        """FR-5: `/UpdateDBCfg -Extension` вернул ненулевой код, а `/Out`-лог пуст.

        Платформа `CONFIG /UpdateDBCfg` при успешном обновлении может завершиться
        не кодом `0` (I-4: статус решает лог, а не код возврата). Если процесс вышел
        и лог пуст — исполнитель всё равно фиксирует успех, иначе сервер счёл бы
        «запуск не состоялся» и закрыл задачу `failed`.
        """
        log_path = work_dir / "log-update-db-extension.txt"
        provider = NonZeroExitProvider()  # код 5 и пустой лог

        async with agent_api(one_c_config, provider) as (http, app):
            response = await http.post(
                "/run-update-db",
                json={
                    **UPDATE_BODY,
                    "is_fast_update": True,
                    "extension_name": "БГ_Торговоя",
                },
                headers=HEADERS,
            )
            assert response.status_code == 202
            assert log_path.read_text(encoding="utf-8") == ""

            await app.state.agent_runner.wait_idle()

            check = await http.get(
                "/check-update-db",
                params={"extension": "true"},
                headers=HEADERS,
            )

        assert any("/UpdateDBCfg" in command for command in provider.commands)
        assert log_path.read_text(encoding="utf-8") == SUCCESS_UPDATE_MSG
        assert check.status_code == 200, "пустой лог после успешного процесса — успех, не «202»"
        assert check.json()["status"] == "success"

    async def test_run_update_db_bom_only_log_is_marked_success(
        self, one_c_config, work_dir: Path
    ) -> None:  # noqa: ANN001
        """FR-5: лог содержит только BOM (U+FEFF) — фолбэк всё равно дописывает успех.

        1С при `/UpdateDBCfg -Extension` создаёт файл с BOM-заголовком, но без текста.
        `str.strip()` BOM не удаляет, поэтому без явного учёта BOM фолбэк считал бы
        лог непустым и не дописал бы строку успеха — задача падала бы как
        «запуск не состоялся» (лог «Process in progress... <BOM>»).
        """
        log_path = work_dir / "log-update-db-extension.txt"
        provider = BomOnlyExitProvider()  # ненулевой код и лог из одного BOM

        async with agent_api(one_c_config, provider) as (http, app):
            response = await http.post(
                "/run-update-db",
                json={
                    **UPDATE_BODY,
                    "is_fast_update": True,
                    "extension_name": "БГ_Торговоя",
                },
                headers=HEADERS,
            )
            assert response.status_code == 202

            await app.state.agent_runner.wait_idle()

            check = await http.get(
                "/check-update-db",
                params={"extension": "true"},
                headers=HEADERS,
            )

        assert log_path.read_text(encoding="utf-8") == SUCCESS_UPDATE_MSG
        assert check.status_code == 200, "BOM-лог после успешного процесса — успех, не «202»"
        assert check.json()["status"] == "success"

    async def test_check_update_db_states(self, one_c_config, work_dir: Path) -> None:  # noqa: ANN001
        log_path = work_dir / "log-update-db.txt"

        async with agent_api(one_c_config, RecordingProvider()) as (http, _):
            missing = await http.get("/check-update-db", headers=HEADERS)
            assert missing.status_code == 202

            log_path.write_text("Ошибка обновления", encoding="utf-8")
            failed = await http.get("/check-update-db", headers=HEADERS)
            assert failed.status_code == 400

            log_path.write_text(SUCCESS_UPDATE_MSG, encoding="utf-8")
            success = await http.get("/check-update-db", headers=HEADERS)

        assert success.status_code == 200
        assert success.json()["status"] == "success"

    def test_update_db_rejects_empty_user_name(self, one_c_client) -> None:  # noqa: ANN001
        response = one_c_client.post(
            "/run-update-db", json={**UPDATE_BODY, "user_name": "  "}, headers=HEADERS
        )
        assert response.status_code == 422


class TestDownloadAndMessageEndpoints:
    """`/download-file` (FR-1, ФН2) и `/send-message` (FR-8)."""

    async def test_download_file_accepts_alias_and_schedules_task(
        self, one_c_config, monkeypatch  # noqa: ANN001
    ) -> None:
        calls: list[str] = []

        async def fake_download(public_key: str, *, work_dir: Path, **kwargs: object):
            calls.append(public_key)
            return work_dir / "update.zip"

        monkeypatch.setattr(downloader, "download_file_from_yandex_disk", fake_download)

        async with agent_api(one_c_config, RecordingProvider()) as (http, _):
            response = await http.post(
                "/download-file",
                json={"url-file": "https://disk.yandex.ru/d/abc"},
                headers=HEADERS,
            )
            await wait_until(lambda: calls)

        assert response.status_code == 202
        assert calls == ["https://disk.yandex.ru/d/abc"]

    async def test_download_file_accepts_snake_case(
        self, one_c_config, monkeypatch  # noqa: ANN001
    ) -> None:
        calls: list[str] = []

        async def fake_download(public_key: str, *, work_dir: Path, **kwargs: object):
            calls.append(public_key)
            return work_dir / "update.zip"

        monkeypatch.setattr(downloader, "download_file_from_yandex_disk", fake_download)

        async with agent_api(one_c_config, RecordingProvider()) as (http, _):
            response = await http.post(
                "/download-file",
                json={"url_file": "https://disk.yandex.ru/d/abc"},
                headers=HEADERS,
            )
            await wait_until(lambda: calls)

        assert response.status_code == 202
        assert calls == ["https://disk.yandex.ru/d/abc"]

    async def test_send_message_shows_text_and_kills_1c(
        self, one_c_config, fake_provider: RecordingProvider
    ) -> None:  # noqa: ANN001
        async with agent_api(one_c_config, fake_provider) as (http, _):
            response = await http.post(
                "/send-message",
                json={"text_message": "База будет обновлена через 5 минут"},
                headers=HEADERS,
            )

        assert response.status_code == 200
        assert response.json()["killed_1cv8"] is True, "§5.1.1 п. 4: `taskkill` отработал"
        assert [
            "msg",
            "*/localhost",
            "База будет обновлена через 5 минут",
        ] in fake_provider.quiet_commands
        assert ["taskkill", "/f", "/im", "1cv8.exe"] in fake_provider.quiet_commands

    async def test_send_message_reports_missing_taskkill(self, one_c_config) -> None:  # noqa: ANN001
        """Если утилита недоступна, сервер видит `killed_1cv8=false`, а не «всё хорошо»."""
        provider = RecordingProvider(quiet_available=False)

        async with agent_api(one_c_config, provider) as (http, _):
            response = await http.post(
                "/send-message", json={"text_message": "Обновление"}, headers=HEADERS
            )

        assert response.status_code == 200
        assert response.json()["killed_1cv8"] is False

    def test_send_message_rejects_empty_text(self, one_c_client) -> None:  # noqa: ANN001
        response = one_c_client.post(
            "/send-message", json={"text_message": "   "}, headers=HEADERS
        )
        assert response.status_code == 422



class TestDeleteFilesEndpoint:
    """Э10: `POST /files/delete` — удаление файлов обновления в рабочей папке (§11.3)."""

    def test_deletes_update_files_and_keeps_logs(  # noqa: ANN001
        self, one_c_client, work_dir: Path
    ) -> None:
        for suffix in (".cf", ".cfe", ".xml"):
            (work_dir / f"update{suffix}").write_bytes(b"data")
        log_path = work_dir / "log-merge.txt"
        log_path.write_text("журнал", encoding="utf-8")

        response = one_c_client.post(
            "/files/delete", json={"file_name": "update"}, headers=HEADERS
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert sorted(body["removed"]) == ["update.cf", "update.cfe", "update.xml"]
        assert body["missing"] == []
        assert body["work_dir"] == str(work_dir)
        assert log_path.is_file(), "логи 1С удаление не трогает"
        assert not (work_dir / "update.cf").exists()

    def test_suffix_narrows_the_target(  # noqa: ANN001
        self, one_c_client, work_dir: Path
    ) -> None:
        (work_dir / "update.cf").write_bytes(b"data")

        response = one_c_client.post(
            "/files/delete",
            json={"file_name": "update", "suffix": ".cfe"},
            headers=HEADERS,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["removed"] == []
        assert body["missing"] == ["update.cfe"]
        assert (work_dir / "update.cf").is_file(), "чужое расширение не удаляем"

    def test_rejects_traversal_and_unknown_suffix(  # noqa: ANN001
        self, one_c_client
    ) -> None:
        traversal = one_c_client.post(
            "/files/delete", json={"file_name": "..\\1cv8"}, headers=HEADERS
        )
        bad_suffix = one_c_client.post(
            "/files/delete",
            json={"file_name": "update", "suffix": ".exe"},
            headers=HEADERS,
        )

        assert traversal.status_code == 422, "I-2: имя файла без разделителей пути"
        assert bad_suffix.status_code == 422, "удаляем только .cf/.cfe/.xml"

    def test_requires_agent_token(self, one_c_client, work_dir: Path) -> None:  # noqa: ANN001
        (work_dir / "update.cf").write_bytes(b"data")

        response = one_c_client.post("/files/delete", json={"file_name": "update"})

        assert response.status_code == 401
        assert (work_dir / "update.cf").is_file(), "без токена ничего не удаляется"

