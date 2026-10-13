"""Провайдер внешних процессов агента (§7.5: `1cv8.exe`, `msg`, `taskkill`).

Все внешние процессы запускаются только argv-списком через
`asyncio.create_subprocess_exec` (I-1: shell не используется нигде) и только через
этот провайдер — значит в тестах их можно подменить моком, не поднимая реальную
1С и не показывая окон `msg.exe`.

Модуль намеренно ничего не логирует сам: команда 1С содержит пароль, а логировать
её безопасно только через `one_c.format_command()` (I-7), поэтому это делает
вызывающая сторона.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_PROVIDER", "ProcessProvider", "SubprocessProvider"]


@runtime_checkable
class ProcessProvider(Protocol):
    """Интерфейс запуска внешних процессов (в тестах подменяется моком)."""

    async def run_captured(
        self, args: list[str], *, log_path: Path | None = None, timeout: float | None = None
    ) -> int | None:
        """Запустить процесс, вычитать stdout/stderr; `None` — процесс не завершился."""

    async def run_quiet(self, args: list[str]) -> bool:
        """Запустить процесс без вывода; `False` — утилита недоступна."""


class SubprocessProvider:
    """Боевой провайдер: `asyncio.create_subprocess_exec` без командной оболочки."""

    async def run_captured(
        self, args: list[str], *, log_path: Path | None = None, timeout: float | None = None
    ) -> int | None:
        """Запустить процесс 1С, дождаться завершения и дописать вывод в лог (I-6).

        Вывод читается всегда (`communicate()`), иначе процесс «зависнет на пайпах».
        При превышении `timeout` процесс остаётся работать, а мы возвращаем `None`
        — как в v2.
        """
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        communicate = process.communicate()
        try:
            if timeout is None:
                stdout, _ = await communicate
            else:
                stdout, _ = await asyncio.wait_for(communicate, timeout)
        except asyncio.TimeoutError:
            logger.warning("Команда не завершилась за %.0f сек., ждём её в фоне", timeout)
            return None

        output = (stdout or b"").decode("utf-8", errors="ignore")
        if output.strip():
            logger.info("Вывод команды: %s", output.strip())
            if log_path is not None:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(output)

        if process.returncode:
            logger.warning("Команда завершилась с кодом %s", process.returncode)
        return process.returncode

    async def run_quiet(self, args: list[str]) -> bool:
        """Запустить утилиту без вывода (`msg`, `taskkill`); `False` — её нет в системе."""
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.warning("Утилита недоступна: %s", args[0])
            return False
        await process.wait()
        return True


#: Провайдер по умолчанию: чистая фабрика без сайд-эффектов на импорте (I-10)
DEFAULT_PROVIDER: ProcessProvider = SubprocessProvider()
