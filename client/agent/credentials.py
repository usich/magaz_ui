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

import ctypes
import logging
import sys
from collections.abc import Callable
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from client.agent.config import OneCProfile, get_application_root, save_one_c_profile
from shared.schemas import CredentialsPush

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_KEY_FILE_NAME",
    "CredentialCipher",
    "Decryptor",
    "OneCProfileStore",
    "apply_profile_password",
    "dpapi_available",
    "resolve_password",
]

#: Расшифровка `password_enc` (Fernet + ключ под DPAPI машины) — подключается на Э4
Decryptor = Callable[[str], "str | None"]

#: Файл ключа шифрования профиля 1С лежит рядом с конфигом агента (§7.2, §8.3)
DEFAULT_KEY_FILE_NAME = ".credentials.key"


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


# --- Шифрование профиля 1С: Fernet + DPAPI машины (§8.3) --------------------


def dpapi_available() -> bool:
    """Есть ли DPAPI (Windows). Вне Windows ключ хранится просто в файле."""
    return sys.platform == "win32"


class _DataBlob(ctypes.Structure):
    """`DATA_BLOB` из Win32 (`CryptProtectData` / `CryptUnprotectData`)."""

    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    """Защитить/снять защиту данных ключом машины (`CRYPTPROTECT_LOCAL_MACHINE`).

    Ключ профиля 1С защищён DPAPI именно этой кассы (§8.3): файл
    `.credentials.key`, скопированный на другую машину, расшифровать нельзя.
    Служба работает под `LocalSystem`, поэтому используется машинная область,
    а не пользовательская.
    """
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    buffer_in = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buffer_in, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    scope = 0x4  # CRYPTPROTECT_LOCAL_MACHINE

    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not call(ctypes.byref(blob_in), None, None, None, None, scope, ctypes.byref(blob_out)):
        raise OSError("DPAPI недоступен: нет прав или ключ создан другой машиной")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


class CredentialCipher:
    """Fernet-шифрование пароля 1С с ключом, защищённым DPAPI машины (§8.3)."""

    #: Маркер защищённого ключа в файле: `dpapi:<blob>`
    DPAPI_PREFIX = b"dpapi:"

    def __init__(self, key_path: Path) -> None:
        self.key_path = Path(key_path)
        self._fernet: Fernet | None = None

    @classmethod
    def next_to(cls, config_path: Path | str | None) -> "CredentialCipher":
        """Ключ рядом с `agent-config.json` (или в корне приложения, §7.1)."""
        base = Path(config_path).parent if config_path else get_application_root()
        return cls(base / DEFAULT_KEY_FILE_NAME)

    @property
    def fernet(self) -> Fernet:
        """Fernet с ключом профиля (ключ создаётся при первом обращении)."""
        if self._fernet is None:
            self._fernet = Fernet(self._load_key())
        return self._fernet

    def _load_key(self) -> bytes:
        if self.key_path.is_file():
            raw = self.key_path.read_bytes().strip()
            if not raw.startswith(self.DPAPI_PREFIX):
                # Ключ лежит открыто: так работает прогон вне Windows (§7.5)
                return raw
            if not dpapi_available():
                raise OSError("Ключ профиля 1С защищён DPAPI и не читается вне Windows")
            return _dpapi(raw[len(self.DPAPI_PREFIX) :], protect=False)

        key = Fernet.generate_key()
        self._store_key(key)
        return key

    def _store_key(self, key: bytes) -> None:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if dpapi_available():
            payload = self.DPAPI_PREFIX + _dpapi(key, protect=True)
        else:
            payload = key
            logger.warning(
                "DPAPI недоступен (не Windows): ключ профиля 1С хранится без защиты машины"
            )
        self.key_path.write_bytes(payload)

    def encrypt(self, value: str) -> str:
        """Зашифровать пароль 1С для `one_c_profile.password_enc` (§7.2)."""
        return self.fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str | None) -> str | None:
        """Расшифровать `password_enc`; нет ключа/битый токен → `None` + предупреждение."""
        if not token:
            return None
        try:
            return self.fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            logger.warning("Пароль 1С из профиля не расшифрован: ключ профиля не подходит")
            return None


class OneCProfileStore:
    """Профиль 1С агента: приём push-а `POST /credentials` и подстановка пароля (П-1).

    Одна точка на все 1С-операции: `prepare()` подставляет пароль из профиля,
    если сервер не передал его в теле команды (§5.1.1, п. 2 расширений), а
    `apply_push()` принимает учётные данные от сервера и сохраняет их в
    `agent-config.json` (§7.2). Значение пароля нигде не логируется (I-7).
    """

    def __init__(
        self,
        profile: OneCProfile,
        *,
        cipher: CredentialCipher | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.config_path = Path(config_path) if config_path else None
        self.cipher = cipher or CredentialCipher.next_to(self.config_path)
        self._profile = profile

    @property
    def profile(self) -> OneCProfile:
        """Текущий профиль 1С (логин, зашифрованный пароль, версия)."""
        return self._profile

    def resolve(self, password: str) -> str:
        """Пароль для команды 1С: из тела запроса, иначе — из профиля."""
        return resolve_password(password, self._profile, decryptor=self.cipher.decrypt)

    def prepare(self, params):  # noqa: ANN001, ANN201 - pydantic-модель 1С
        """Параметры операции с паролем из профиля, если он не передан явно."""
        return apply_profile_password(params, self._profile, decryptor=self.cipher.decrypt)

    def apply_push(self, payload: CredentialsPush) -> OneCProfile:
        """Принять push учётных данных 1С и сохранить их (§5.1.2, П-1).

        Пустой пароль — законный случай «аутентификация не нужна»: тогда
        `password_enc` очищается, а версия всё равно растёт, чтобы агент
        перестал докладывать расхождение в `/health`.
        """
        user_name = (payload.user_name or "").strip()
        if not user_name:
            raise ValueError("В push-е учётных данных не передан логин 1С")

        password_enc = self.cipher.encrypt(payload.password) if payload.password else None
        self._profile = OneCProfile(
            user_name=user_name,
            password_enc=password_enc,
            credentials_version=max(1, int(payload.credentials_version or 1)),
        )
        if self.config_path is not None and self.config_path.is_file():
            save_one_c_profile(self.config_path, self._profile)

        logger.info(
            "Учётные данные 1С приняты: логин %s, версия %s, пароль %s",
            user_name,
            self._profile.credentials_version,
            "задан" if password_enc else "не задан",
        )
        return self._profile

