"""Ядро 1С на агенте (`client/agent/one_c.py`) — перенос тестов v2 (§9.1).

Проверяется ровно то, за что отвечает перенос: состав команд (FR-3…FR-5),
определение статуса по содержимому лога (FR-7, I-4), обнуление лога до запуска
(I-3), отсутствие пароля в логах (I-7) и путь к платформе из конфига (I-14).
Ни один тест не запускает `1cv8.exe`: провайдер процессов подменён (§7.5).
"""

from __future__ import annotations

from pathlib import Path

from client.agent.one_c import (
    LOG_STATE_FAILED,
    LOG_STATE_IN_PROGRESS,
    LOG_STATE_NOT_FOUND,
    LOG_STATE_SUCCESS,
    SUCCESS_MERGE_MSG,
    SUCCESS_UPDATE_MSG,
    build_enable_users_command,
    build_merge_command,
    build_shutdown_users_command,
    build_update_db_command,
    check_log_file,
    clear_log_file,
    format_command,
    mask_command,
    run_1c_command,
)
from shared.schemas import MergeParams, UpdateParams
from tests.client.conftest import RECORDED_OUTPUT

LOG_PATH = Path("C:/work/log-merge.txt")


def make_merge_params(**overrides) -> MergeParams:  # noqa: ANN003 - удобство тестов
    """Параметры слияния такими, какими их присылает сервер (§5.1.1)."""
    data = {
        "server_name": "srv-1c",
        "base_name": "trade",
        "version_1c": "8.3.20.1234",
        "user_name": "Администратор",
        "password": "123",
        "file_name": "1cv8",
    }
    data.update(overrides)
    return MergeParams(**data)


def make_update_params(**overrides) -> UpdateParams:  # noqa: ANN003 - удобство тестов
    """Параметры обновления БД (§5.1.1)."""
    data = {
        "server_name": "srv-1c",
        "base_name": "trade",
        "version_1c": "8.3.20.1234",
        "user_name": "Администратор",
        "password": "123",
    }
    data.update(overrides)
    return UpdateParams(**data)


class TestBuildMergeCommand:
    """`build_merge_command` собирает argv-список (перенос v2: FR-3, FR-4)."""

    def test_main_configuration(self, one_c_paths) -> None:  # noqa: ANN001 - fixture
        args = build_merge_command(make_merge_params(), LOG_PATH, one_c_paths)

        assert args[0].endswith("bin\\1cv8.exe") or args[0].endswith("bin/1cv8.exe")
        assert "CONFIG" in args
        assert "/Ssrv-1c\\trade" in args
        assert "/NАдминистратор" in args
        assert "/P123" in args
        assert "/DisableStartupMessages" in args, "I-5: ключ есть в каждой команде"
        assert "/MergeCfg" in args
        assert "-force" in args
        assert "-Settings" in args
        assert "/UCПакетноеОбновлениеКонфигурацииИБ" in args
        assert "/Extension" not in args
        assert args[args.index("/Out") + 1] == str(LOG_PATH)
        assert args[args.index("-Settings") + 1].endswith("1cv8.xml")
        assert args[args.index("/MergeCfg") + 1].endswith("1cv8.cf")

    def test_full_merge_uses_full_merge_xml(self, one_c_paths) -> None:  # noqa: ANN001
        args = build_merge_command(make_merge_params(full_merge=True), LOG_PATH, one_c_paths)
        assert args[args.index("-Settings") + 1].endswith("full_merge.xml")

    def test_extension_uses_cfe_and_extension_key(self, one_c_paths) -> None:  # noqa: ANN001
        args = build_merge_command(
            make_merge_params(extension_name="МоеРасширение"), LOG_PATH, one_c_paths
        )

        # Расширение 1С объединяется `/MergeCfg <файл>.cfe -Extension <имя> -force
        # -Settings <файл>.xml` (проверено на 8.3.27). По умолчанию settings —
        # `full_merge.xml`; `/LoadCfg` для расширения не используется.
        assert "/MergeCfg" in args
        assert args[args.index("/MergeCfg") + 1].endswith("1cv8.cfe")
        assert args[args.index("-Extension") + 1] == "МоеРасширение"
        assert "/LoadCfg" not in args
        assert "-force" in args, "FR-4: -force из рабочего вызова на 8.3.27"
        assert "-Settings" in args, "FR-4: расширение тоже идёт через -Settings"
        assert args[args.index("-Settings") + 1].endswith("full_merge.xml"), (
            "FR-4: для расширения по умолчанию full_merge.xml"
        )
        # Порядок как в успешном вызове: файл → -Extension → -force → -Settings.
        assert args.index("-Extension") < args.index("-force") < args.index("-Settings")

    def test_extension_partial_merge_uses_settings_file(self, one_c_paths) -> None:  # noqa: ANN001
        args = build_merge_command(
            make_merge_params(
                extension_name="МоеРасширение", settings_file_name="частичное_слияние"
            ),
            LOG_PATH,
            one_c_paths,
        )
        assert args[args.index("-Settings") + 1].endswith("частичное_слияние.xml"), (
            "FR-4: частичное слияние расширения использует заданное имя файла настроек"
        )

    def test_empty_password_is_not_passed(self, one_c_paths) -> None:  # noqa: ANN001
        args = build_merge_command(make_merge_params(password=""), LOG_PATH, one_c_paths)
        assert not any(arg.startswith("/P") for arg in args)

    def test_shell_metacharacters_stay_inside_one_argument(self, one_c_paths) -> None:  # noqa: ANN001
        """Параметры не могут добавить новую команду: запуск идёт без shell (I-1)."""
        args = build_merge_command(
            make_merge_params(server_name="srv & whoami", user_name="user | calc"),
            LOG_PATH,
            one_c_paths,
        )
        assert "/Ssrv & whoami\\trade" in args
        assert "/Nuser | calc" in args

    def test_format_command_for_logs(self, one_c_paths) -> None:  # noqa: ANN001
        rendered = format_command(build_merge_command(make_merge_params(), LOG_PATH, one_c_paths))
        assert "1cv8.exe" in rendered
        assert "/MergeCfg" in rendered
        assert "/MergeCfg" in rendered.split()

    def test_password_is_masked_in_logs(self, one_c_paths) -> None:  # noqa: ANN001
        """I-7: в логе команда есть, пароля в ней нет."""
        args = build_merge_command(make_merge_params(password="Секрет1С"), LOG_PATH, one_c_paths)
        rendered = format_command(args)

        assert "Секрет1С" not in rendered
        assert "/P***" in mask_command(args)

    def test_executable_path_comes_from_config(self, one_c_paths) -> None:  # noqa: ANN001
        """I-14: путь к платформе — из конфига, а не из хардкода."""
        args = build_merge_command(make_merge_params(version_1c="8.3.24.1548"), LOG_PATH, one_c_paths)
        assert str(one_c_paths.base_1c_path) in args[0]
        assert "8.3.24.1548" in args[0]


class TestBuildUpdateAndServiceCommands:
    """Команды обновления БД и работы с сеансами пользователей (FR-5, FR-6)."""

    def test_update_db_command(self, one_c_paths) -> None:  # noqa: ANN001
        args = build_update_db_command(make_update_params(), LOG_PATH, one_c_paths)

        assert "CONFIG" in args
        assert "/UpdateDBCfg" in args
        assert "/UCКодРазрешения" in args
        assert "/MergeCfg" not in args
        assert args[args.index("/Out") + 1] == str(LOG_PATH)

    def test_update_db_extension_command(self, one_c_paths) -> None:  # noqa: ANN001
        args = build_update_db_command(
            make_update_params(extension_name="МоеРасширение"), LOG_PATH, one_c_paths
        )
        assert "/Extension" in args
        assert args[args.index("/Extension") + 1] == "МоеРасширение"

    def test_shutdown_users_command(self, one_c_paths) -> None:  # noqa: ANN001
        args = build_shutdown_users_command(make_update_params(), LOG_PATH, one_c_paths)

        assert "ENTERPRISE" in args
        assert "/CЗавершитьРаботуПользователей" in args
        assert "/WA-" in args
        assert "/AU-" in args
        assert args[args.index("/Out") + 1] == str(LOG_PATH)

    def test_enable_users_command(self, one_c_paths) -> None:  # noqa: ANN001
        args = build_enable_users_command(make_update_params(), LOG_PATH, one_c_paths)

        assert "ENTERPRISE" in args
        assert "/CРазрешитьРаботуПользователей" in args
        assert "/UCКодРазрешения" in args
        assert args[args.index("/Out") + 1] == str(LOG_PATH)


class TestLogFileState:
    """`clear_log_file` / `check_log_file` (перенос v2: FR-7, I-3, I-4)."""

    async def test_not_found_state(self, one_c_paths) -> None:  # noqa: ANN001
        state, message = await check_log_file(one_c_paths.log_merge, SUCCESS_MERGE_MSG)
        assert state == LOG_STATE_NOT_FOUND
        assert message == "Log file not found."

    async def test_empty_log_means_in_progress(self, one_c_paths) -> None:  # noqa: ANN001
        await clear_log_file(one_c_paths.log_merge)
        assert one_c_paths.log_merge.exists()
        assert one_c_paths.log_merge.read_text(encoding="utf-8") == ""

        state, _ = await check_log_file(one_c_paths.log_merge, SUCCESS_MERGE_MSG)
        assert state == LOG_STATE_IN_PROGRESS

    async def test_success_state(self, one_c_paths) -> None:  # noqa: ANN001
        one_c_paths.log_merge.write_text(SUCCESS_MERGE_MSG, encoding="utf-8")
        state, logs = await check_log_file(one_c_paths.log_merge, SUCCESS_MERGE_MSG)
        assert state == LOG_STATE_SUCCESS
        assert logs == SUCCESS_MERGE_MSG

    async def test_failed_state(self, one_c_paths) -> None:  # noqa: ANN001
        one_c_paths.log_merge.write_text("Ошибка загрузки конфигурации", encoding="utf-8")
        state, logs = await check_log_file(one_c_paths.log_merge, SUCCESS_MERGE_MSG)
        assert state == LOG_STATE_FAILED
        assert "Ошибка" in logs

    async def test_english_error_marker_is_failure(self, one_c_paths) -> None:  # noqa: ANN001
        """`ERROR_MARKERS` = («Ошибка», «Error») — перенос v2 (I-4)."""
        one_c_paths.log_merge.write_text("Error: file not found", encoding="utf-8")
        state, _ = await check_log_file(one_c_paths.log_merge, SUCCESS_MERGE_MSG)
        assert state == LOG_STATE_FAILED

    async def test_in_progress_state_for_unknown_content(self, one_c_paths) -> None:  # noqa: ANN001
        one_c_paths.log_merge.write_text("Слияние выполняется...", encoding="utf-8")
        state, _ = await check_log_file(one_c_paths.log_merge, SUCCESS_MERGE_MSG)
        assert state == LOG_STATE_IN_PROGRESS

    async def test_update_success_message_is_used(self, one_c_paths) -> None:  # noqa: ANN001
        one_c_paths.log_merge.write_text(SUCCESS_UPDATE_MSG, encoding="utf-8")
        state, _ = await check_log_file(one_c_paths.log_merge, SUCCESS_UPDATE_MSG)
        assert state == LOG_STATE_SUCCESS

        state, _ = await check_log_file(one_c_paths.log_merge, SUCCESS_MERGE_MSG)
        assert state == LOG_STATE_IN_PROGRESS, "чужой текст успеха успехом не считается"

    async def test_clear_log_file_overwrites_content(self, one_c_paths) -> None:  # noqa: ANN001
        one_c_paths.log_merge.write_text(SUCCESS_MERGE_MSG, encoding="utf-8")
        await clear_log_file(one_c_paths.log_merge)
        assert one_c_paths.log_merge.read_text(encoding="utf-8") == ""

    async def test_extension_merge_has_its_own_log(self, one_c_paths) -> None:  # noqa: ANN001
        """§7.1: имена логов — как в v2, у расширения свой файл."""
        assert one_c_paths.merge_log("Расширение") == one_c_paths.log_merge_extension
        assert one_c_paths.merge_log(None) == one_c_paths.log_merge
        assert one_c_paths.log_merge_extension.name == "log-merge-extension.txt"
        assert one_c_paths.update_log("Расширение").name == "log-update-db-extension.txt"


class TestRun1CCommand:
    """`run_1c_command` — I-1 (без shell), I-6 (вывод вычитывается), I-7."""

    async def test_output_goes_to_executor_log(self, one_c_paths, fake_provider) -> None:  # noqa: ANN001
        code = await run_1c_command(
            ["1cv8.exe", "CONFIG"], one_c_paths.log_executor, provider=fake_provider
        )

        assert code == 0
        assert fake_provider.commands == [["1cv8.exe", "CONFIG"]]
        assert one_c_paths.log_executor.name == "log-1cv8-console.txt"
        assert RECORDED_OUTPUT.strip() in one_c_paths.log_executor.read_text(encoding="utf-8")

    async def test_arguments_are_passed_as_list(self, one_c_paths, fake_provider) -> None:  # noqa: ANN001
        """I-1: провайдер получает argv-список, а не строку для shell."""
        await run_1c_command(["1cv8.exe", "/Ssrv\\base"], provider=fake_provider)
        assert fake_provider.commands[0] == ["1cv8.exe", "/Ssrv\\base"]
