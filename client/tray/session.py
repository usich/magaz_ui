"""Сессия Windows, от имени которой работает TrayAgent (§7.4, Q50, П-6).

Учёт ведётся **по сессии**, а не по сотруднику: справочника сотрудников нет
(П-6), и «ознакомлен» ставит тот, кто в этот момент работает за кассой. Поэтому
идентификатор сессии обязан совпадать с тем, что видит служба и сервер.

Штатный путь на Windows — `ProcessIdToSessionId`; переменные окружения
`MAGAZ_TRAY_SESSION` / `MAGAZ_TRAY_USER` нужны для отладки (`--console`) и
тестов: с ними сессию можно подменить, не трогая код.
"""

from __future__ import annotations

import getpass
import os
import sys

from shared.schemas import SessionRef

__all__ = [
    "SESSION_ENV",
    "USER_ENV",
    "current_session_id",
    "current_session_user",
    "session_ref",
]

#: Отладочное переопределение сессии и логина (иначе — реальные значения ОС)
SESSION_ENV = "MAGAZ_TRAY_SESSION"
USER_ENV = "MAGAZ_TRAY_USER"

#: Сессия, которую трей сообщает, если ОС не смогла её назвать
UNKNOWN_SESSION_ID = "0"


def current_session_id() -> str:
    """Идентификатор сессии Windows текущего процесса (`str`, как в API).

    Вызов `ProcessIdToSessionId` — единственный способ получить ту же сессию,
    которую видит Планировщик; при неудаче (не Windows, урезанные права)
    возвращается `UNKNOWN_SESSION_ID`: служба отличит такую сессию от живой по
    времени последнего касания и всё равно покажет уведомление.
    """
    override = os.environ.get(SESSION_ENV, "").strip()
    if override:
        return override

    if sys.platform == "win32":  # pragma: no cover - проверяется на кассе (Э7)
        import ctypes

        session_id = ctypes.c_ulong()
        try:
            ok = ctypes.windll.kernel32.ProcessIdToSessionId(  # type: ignore[attr-defined]
                os.getpid(), ctypes.byref(session_id)
            )
        except (AttributeError, OSError):
            ok = 0
        if ok:
            return str(session_id.value)

    return UNKNOWN_SESSION_ID


def current_session_user() -> str:
    """Логин Windows текущего пользователя (адресат уведомления, П-6)."""
    override = os.environ.get(USER_ENV, "").strip()
    if override:
        return override
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - служба без окружения пользователя
        return os.environ.get("USERNAME", "user") or "user"


def session_ref() -> SessionRef:
    """Ссылка на сессию для запросов `GET /notifications/pending` и `ack` (§5.1.2)."""
    return SessionRef(session_id=current_session_id(), session_user=current_session_user())
