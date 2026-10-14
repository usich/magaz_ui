"""Предполётная проверка готовности к приёмке (Э7): общий формат отчёта.

Проверка запускается **на чужой машине** (сервер заказчика и касса) и отвечает на
один вопрос: «можно ли начинать приёмку». Поэтому результат — не исключение, а
список независимых пунктов с тремя исходами:

* `ok` — готово;
* `warn` — работать можно, но на это стоит посмотреть до стенда;
* `fail` — идти дальше нельзя (и код возврата процесса ненулевой).

Наборы проверок объявляют `server/preflight.py` и `client/agent/preflight.py`;
здесь только представление результата, чтобы вывод сервера и кассы выглядел
одинаково и легко прикладывался к протоколу `docs/ACCEPTANCE.md`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = [
    "CHECK_FAIL",
    "CHECK_LABELS",
    "CHECK_OK",
    "CHECK_WARN",
    "CheckResult",
    "PreflightReport",
    "check",
    "console_safe",
]

#: Исходы проверок: строками, чтобы отчёт читался и в JSON без преобразований
CHECK_OK = "ok"
CHECK_WARN = "warn"
CHECK_FAIL = "fail"

#: Подписи для человекочитаемого вывода
CHECK_LABELS: dict[str, str] = {
    CHECK_OK: "OK",
    CHECK_WARN: "ВНИМАНИЕ",
    CHECK_FAIL: "ОШИБКА",
}


def check(name: str, status: str, detail: str = "", hint: str = "") -> "CheckResult":
    """Собрать один пункт проверки (короткая форма для списков проверок)."""
    return CheckResult(name=name, status=status, detail=detail, hint=hint)


def console_safe(text: str, encoding: str | None = None) -> str:
    """Привести текст к кодировке консоли (на русском Windows — cp1251).

    Живой дефект Э7: `print(report.render())` падал с `UnicodeEncodeError` на кассе
    из-за символов `→` и `≥` — то есть проверка, призванная показать проблему,
    сама становилась проблемой. Здесь символы, которых нет в кодировке вывода,
    заменяются на `?`: текст остаётся читаемым, приёмка не останавливается.
    """
    target = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(target)
    except LookupError:
        return text  # кодировка неизвестна — отдаём текст как есть, не гадаем
    except UnicodeEncodeError:
        return text.encode(target, "replace").decode(target, "replace")
    return text


@dataclass(frozen=True)
class CheckResult:
    """Один пункт предполётной проверки.

    `hint` — что делать: предполётная проверка бесполезна, если после «ОШИБКА»
    непонятно, куда идти (обычно это ссылка на раздел INSTALL/RUNBOOK).
    """

    name: str
    status: str
    detail: str = ""
    hint: str = ""

    @property
    def is_failure(self) -> bool:
        """Пункт блокирует приёмку."""
        return self.status == CHECK_FAIL

    @property
    def is_warning(self) -> bool:
        """Пункт не блокирует, но требует внимания."""
        return self.status == CHECK_WARN

    @property
    def label(self) -> str:
        """Подпись статуса для вывода."""
        return CHECK_LABELS.get(self.status, self.status)

    def as_dict(self) -> dict[str, Any]:
        """Пункт в виде словаря — для сохранения отчёта рядом с протоколом."""
        return {
            "check": self.name,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
        }

    def render(self, *, width: int = 34) -> str:
        """Строка вывода: подпись, пункт, подробности и подсказка.

        Только ASCII в оформлении: отчёт печатают и на кассе, а консоль русского
        Windows работает в cp1251 — символы `→` или `…` уронили бы `print`
        (`UnicodeEncodeError`) ровно в момент приёмки.
        """
        line = f"[{self.label:^8}] {self.name:<{width}} {self.detail}".rstrip()
        if self.hint and self.status in (CHECK_FAIL, CHECK_WARN):
            line = f"{line}\n{'':15}  -> {self.hint}"
        return line


@dataclass
class PreflightReport:
    """Отчёт: список проверок, итог и код возврата процесса.

    `ok` — ни одного `fail`: предупреждения не мешают начинать приёмку, поэтому
    код возврата от них не зависит (иначе скрипты стенда останавливались бы на
    «TrayAgent не зарегистрирован в Планировщике» — это лечится по ходу).
    """

    title: str
    results: list[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> "PreflightReport":
        """Добавить пункт и вернуть отчёт — удобно собирать цепочкой."""
        self.results.append(result)
        return self

    def add_all(self, results: Iterable[CheckResult]) -> "PreflightReport":
        """Добавить несколько пунктов сразу."""
        for result in results:
            self.add(result)
        return self

    @property
    def failures(self) -> list[CheckResult]:
        """Пункты, блокирующие приёмку."""
        return [item for item in self.results if item.is_failure]

    @property
    def warnings(self) -> list[CheckResult]:
        """Пункты, требующие внимания, но не блокирующие."""
        return [item for item in self.results if item.is_warning]

    @property
    def ok(self) -> bool:
        """Можно ли начинать (ошибок нет)."""
        return not self.failures

    @property
    def exit_code(self) -> int:
        """Код возврата процесса: `0` — можно начинать, `1` — есть ошибки."""
        return 0 if self.ok else 1

    def as_dict(self) -> dict[str, Any]:
        """Отчёт как словарь — для JSON-вывода и приложения к протоколу."""
        return {
            "title": self.title,
            "ok": self.ok,
            "counts": {
                CHECK_OK: len(self.results) - len(self.failures) - len(self.warnings),
                CHECK_WARN: len(self.warnings),
                CHECK_FAIL: len(self.failures),
            },
            "checks": [item.as_dict() for item in self.results],
        }

    def summary(self) -> str:
        """Итоговая строка: сколько проверок и чем закончилось."""
        ok_count = len(self.results) - len(self.failures) - len(self.warnings)
        parts = [f"проверок: {len(self.results)}", f"OK: {ok_count}"]
        if self.warnings:
            parts.append(f"внимание: {len(self.warnings)}")
        if self.failures:
            parts.append(f"ошибок: {len(self.failures)}")
        tail = (
            "можно начинать приёмку"
            if self.ok
            else "сначала устраните ошибки (см. docs/INSTALL.md, docs/RUNBOOK.md)"
        )
        return f"{', '.join(parts)} — {tail}"

    def render(self) -> str:
        """Полный вывод отчёта: пункты, итог и перечень ошибок."""
        lines = [f"== {self.title} ==", ""]
        lines.extend(item.render() for item in self.results)
        lines.append("")
        lines.append(self.summary())
        return "\n".join(lines)
