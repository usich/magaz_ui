"""Сообщение пользователям кассы и завершение 1С (FR-8, §5.1.1 `/send-message`).

Перенос v2 1:1: `msg */localhost <текст>` показывает окно во всех локальных
сессиях, затем `taskkill /f /im 1cv8.exe` снимает все процессы 1С (риск R4 —
поэтому сервер обязан предупредить администратора, §14.2).

Обе утилиты запускаются argv-списком без shell (I-1) и через провайдер (§7.5),
поэтому в тестах окна не всплывают. Расширение §5.1.1 (п. 4): наружу отдаётся
признак `killed_1cv8`, чтобы сервер знал, что `taskkill` **реально отработал**
(«процессов не было» тоже считается успехом — утилита запустилась).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from client.agent.processes import DEFAULT_PROVIDER

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    from client.agent.processes import ProcessProvider

logger = logging.getLogger(__name__)

__all__ = [
    "KILL_1CV8_ARGS",
    "MSG_EXE",
    "MSG_TARGET",
    "TASKKILL_EXE",
    "kill_1c_processes",
    "send_windows_message",
    "show_notification_message",
]

#: Утилита показа сообщений и цель «все сессии этой машины» (перенос v2)
MSG_EXE = "msg"
MSG_TARGET = "*/localhost"
#: Утилита снятия процессов 1С (перенос v2)
TASKKILL_EXE = "taskkill"
KILL_1CV8_ARGS = ("/f", "/im", "1cv8.exe")


async def kill_1c_processes(*, provider: ProcessProvider | None = None) -> bool:
    """Завершить все `1cv8.exe` на кассе; `False` — утилита `taskkill` недоступна."""
    active = provider or DEFAULT_PROVIDER
    args = [TASKKILL_EXE, *KILL_1CV8_ARGS]
    logger.info("Завершение процессов 1С: %s", " ".join(args))
    return await active.run_quiet(args)


async def show_notification_message(
    message: str, *, provider: ProcessProvider | None = None
) -> bool:
    """Показать уведомление во всех сессиях через `msg.exe` (ФН3, §7.4).

    Отличие от `/send-message` принципиальное: здесь **нет** `taskkill` —
    уведомление (даже `critical`) не имеет права прерывать работу с 1С. Это
    дублирующий канал: если TrayAgent в сессии не запущен, окно покажет служба.
    Отсутствие `msg.exe` (урезанные сборки Windows, Session 0) — не ошибка.
    """
    active = provider or DEFAULT_PROVIDER
    shown = await active.run_quiet([MSG_EXE, MSG_TARGET, message])
    if not shown:
        logger.warning("Уведомление не продублировано: утилита msg.exe недоступна")
    return shown


async def send_windows_message(
    message: str, *, provider: ProcessProvider | None = None
) -> bool:
    """Показать сообщение во всех сессиях и завершить 1С (FR-8).

    Возвращает `killed_1cv8` для ответа `/send-message` (§5.1.1, п. 4).
    Отсутствие `msg.exe` (урезанные сборки Windows, Session 0) — не ошибка:
    дублирующий канал молчит, основной — TrayAgent (§7.4).
    """
    active = provider or DEFAULT_PROVIDER
    shown = await active.run_quiet([MSG_EXE, MSG_TARGET, message])
    if not shown:
        logger.warning("Сообщение не показано: утилита msg.exe недоступна")
    killed = await kill_1c_processes(provider=active)
    logger.info("Сообщение обработано (msg=%s, killed_1cv8=%s)", shown, killed)
    return killed
