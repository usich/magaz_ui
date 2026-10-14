"""Тесты `shared.preflight` — формат предполётного отчёта (Э7).

Проверяется не «сколько проверок», а поведение отчёта: предупреждения не должны
останавливать приёмку, ошибки — должны, и в выводе должно быть видно, что делать.
"""

from __future__ import annotations

from shared.preflight import (
    CHECK_FAIL,
    CHECK_OK,
    CHECK_WARN,
    CheckResult,
    PreflightReport,
    check,
    console_safe,
)


class TestCheckResult:
    """Один пункт проверки: статус, подпись и подсказка."""

    def test_labels_and_flags(self) -> None:
        assert check("Порт", CHECK_OK).label == "OK"
        assert check("Порт", CHECK_WARN).label == "ВНИМАНИЕ"
        result = check("Порт", CHECK_FAIL, "занят", "освободите порт")
        assert result.is_failure is True
        assert result.is_warning is False

    def test_warning_is_not_failure(self) -> None:
        result = check("msg.exe", CHECK_WARN, "не найден")
        assert result.is_failure is False
        assert result.is_warning is True

    def test_as_dict(self) -> None:
        payload = check("Порт", CHECK_FAIL, "занят", "освободите порт").as_dict()
        assert payload == {
            "check": "Порт",
            "status": CHECK_FAIL,
            "detail": "занят",
            "hint": "освободите порт",
        }

    def test_render_keeps_name_and_detail(self) -> None:
        line = check("Схема БД", CHECK_OK, "таблиц: 11").render()
        assert "Схема БД" in line
        assert "таблиц: 11" in line
        assert "->" not in line  # у успешного пункта подсказки не показываем

    def test_render_shows_hint_for_warnings_and_failures(self) -> None:
        assert "->" in check("msg.exe", CHECK_WARN, "не найден", "это штатно (R2)").render()
        assert "->" in check("Схема БД", CHECK_FAIL, "нет таблиц", "alembic upgrade head").render()

    def test_render_is_printable_on_cp1251_console(self) -> None:
        """Консоль русского Windows — cp1251: отчёт должен печататься без сюрпризов.

        Проверка появилась после живого дефекта: символы `→` и `≥` в подсказках
        роняли `print` с `UnicodeEncodeError` именно на кассе (§14.2).
        """
        text = PreflightReport(title="Проверка").add(
            check("Пункт", CHECK_FAIL, "нет таблиц: a, b, ... ", "alembic upgrade head (§4.1)")
        ).render()
        text.encode("cp1251")  # не должно бросать исключение


class TestConsoleSafe:
    """Страховка на случай, если в отчёт попадёт символ вне кодировки консоли."""

    def test_utf8_console_keeps_text_intact(self) -> None:
        assert console_safe("нужно >= 3.12", "utf-8") == "нужно >= 3.12"

    def test_cp1251_replaces_only_foreign_characters(self) -> None:
        result = console_safe("версия → нужна", "cp1251")
        assert result == "версия ? нужна"

    def test_cp1251_keeps_cyrillic_and_section_sign(self) -> None:
        assert console_safe("Схема БД (§4.1) — ошибка", "cp1251") == "Схема БД (§4.1) — ошибка"

    def test_unknown_encoding_does_not_break_output(self) -> None:
        assert console_safe("проверка", "нет-такой-кодировки") == "проверка"

    def test_render_ignores_hint_without_status(self) -> None:
        assert "подсказка" not in check("Версия", CHECK_OK, "1.10", "подсказка").render()


class TestPreflightReport:
    """Отчёт целиком: итог, код возврата и пригодность для протокола."""

    def test_ok_when_no_failures(self) -> None:
        report = PreflightReport(title="Касса")
        report.add(check("Конфиг", CHECK_OK, "прочитан"))
        report.add(check("msg.exe", CHECK_WARN, "не найден"))
        assert report.ok is True
        assert report.exit_code == 0
        assert len(report.warnings) == 1
        assert report.failures == []

    def test_failure_breaks_the_trip(self) -> None:
        report = PreflightReport(title="Сервер")
        report.add_all([check("Конфиг", CHECK_OK), check("Схема БД", CHECK_FAIL, "нет таблиц")])
        assert report.ok is False
        assert report.exit_code == 1
        assert [item.name for item in report.failures] == ["Схема БД"]

    def test_counts_and_dict(self) -> None:
        report = PreflightReport(title="Сервер")
        report.add_all(
            [
                check("A", CHECK_OK),
                check("B", CHECK_WARN),
                check("C", CHECK_FAIL),
            ]
        )
        payload = report.as_dict()
        assert payload["ok"] is False
        assert payload["counts"] == {CHECK_OK: 1, CHECK_WARN: 1, CHECK_FAIL: 1}
        assert [item["check"] for item in payload["checks"]] == ["A", "B", "C"]

    def test_summary_mentions_result(self) -> None:
        report = PreflightReport(title="Сервер").add(check("A", CHECK_OK))
        assert "можно начинать приёмку" in report.summary()
        report.add(check("B", CHECK_FAIL))
        assert "устраните ошибки" in report.summary()

    def test_render_contains_title_and_all_items(self) -> None:
        report = PreflightReport(title="Предполётная проверка кассы")
        report.add_all([check("Конфиг", CHECK_OK, "прочитан"), check("Интерфейс (SPA)", CHECK_FAIL, "нет")])
        text = report.render()
        assert text.startswith("== Предполётная проверка кассы ==")
        assert "Конфиг" in text
        assert "ОШИБКА" in text
        assert text.rstrip().endswith("(см. docs/INSTALL.md, docs/RUNBOOK.md)")

    def test_add_returns_report_for_chaining(self) -> None:
        report = PreflightReport(title="Сервер")
        assert report.add(CheckResult(name="A", status=CHECK_OK)) is report
