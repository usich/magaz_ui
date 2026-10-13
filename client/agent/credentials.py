"""Подстановка пароля 1С из профиля агента (П-1, §5.1.1 п. 2 расширений).

Пароль может **не приходить** в теле команды: сервер хранит его у себя
(`stores.password_enc`) и доставляет push-ом `POST /credentials`, а агент
складывает зашифрованное значение в `one_c_profile.password_enc` (§7.2).

Здесь единственная точка подстановки — `resolve_password()`: она вызывается
перед сборкой команды 1С. Так у Э4 (push учётных данных и расшифровка
Fernet + DPAPI) есть одно место для подключения, а эндпоинты §5.1.1 не меняются.

Инвариант I-7: пароль никогда не логируется. Ниже в лог попадает только факт
«профиль есть / расшифровать нечем», без значения.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from client.agent.config import OneCProfile

logger = logging.getLogger(__name__)

__all__ = ["Decryptor", "apply_profile_password", "resolve_password"]

#: Расшифровка `password_enc` (Fernet + ключ под DPAPI машины) — подключается на Э4
Decryptor = Callable[[str], "str | None"]


def resolve_password(
    password: str,
    profile: OneCProfile,
    *,
    decryptor: Decryptor | None = None,
) -> str:
    """Пароль для 1С: из тела запроса, иначе — из зашифрованного профиля (П-1).

    Пустая строка означает «пароль не задан» — ровно как в v2: ключ `/P` в команду
    не попадёт, и база ответит своим способом аутентификации.
    """
    if password:
        return password
    if not profile.password_enc:
        return ""
    if decryptor is None:
        logger.warning(
            "Пароль 1С не передан в команде, а профиль агента зашифрован: "
            "расшифровка подключается на этапе Э4 (П-1)"
        )
        return ""
    return decryptor(profile.password_enc) or ""


def apply_profile_password(
    params,
    profile: OneCProfile,
    *,
    decryptor: Decryptor | None = None,
):  # noqa: ANN001, ANN201 - pydantic-модель параметров 1С
    """Копия параметров операции с паролем из профиля, если он не передан явно."""
    resolved = resolve_password(params.password, profile, decryptor=decryptor)
    if resolved == params.password:
        return params
    return params.model_copy(update={"password": resolved})
