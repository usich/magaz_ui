"""Настройки сервера (§14.1 п. 4).

Читаются из переменных окружения с префиксом `MAGAZ_` и/или файла `.env`
в корне репозитория. Значения по умолчанию рассчитаны на локальный запуск:
`python -m server.main` поднимает дашборд на 0.0.0.0:8080.

Инвариант I-10: импорт модуля не имеет сайд-эффектов — конфиг читается явной
функцией `get_settings()`, файлы/каталоги создаются в `lifespan`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.version import APP_NAME, APP_VERSION

# --- Пути ---------------------------------------------------------------

SERVER_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SERVER_ROOT.parent

DATA_DIR = SERVER_ROOT / "data"
LOG_DIR = DATA_DIR / "logs"
MIGRATIONS_DIR = SERVER_ROOT / "migrations"
FRONTEND_DIST_DIR = REPO_ROOT / "frontend" / "dist"
#: Картинки уведомлений (§4.6, Э9): в теле они адресуются меткой
#: `magaz-image:<имя>`, а сам файл лежит здесь — рядом с БД и ключами
NOTIFICATION_IMAGES_DIR = DATA_DIR / "notification-images"

# Файлы секретов (§8.3): ключ шифрования паролей 1С — отдельный от ключа cookie
SECRETS_KEY_PATH = DATA_DIR / "secrets.key"
COOKIE_SECRET_PATH = DATA_DIR / "cookie_secret.key"

#: Имя cookie администратора и путь API (используются и сервером, и тестами)
ADMIN_COOKIE_NAME = "magaz_admin"
API_PREFIX = "/api/v1"


def default_database_url() -> str:
    """SQLite-файл `server\\data\\magaz.db` (§4); для PostgreSQL — строка подключения."""
    db_path = (DATA_DIR / "magaz.db").as_posix()
    return f"sqlite:///{db_path}"


class Settings(BaseSettings):
    """Конфигурация сервера дашборда."""

    model_config = SettingsConfigDict(
        env_prefix="MAGAZ_",
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = APP_NAME
    app_version: str = APP_VERSION

    host: str = "0.0.0.0"
    port: int = 8080
    reload: bool = False

    #: СУБД (Q12): SQLite по умолчанию, PostgreSQL — только строкой подключения
    database_url: str = ""
    sql_echo: bool = False

    #: Секреты: пусто → ключи читаются/создаются в `server/data` (§8.3)
    secret_key: str = ""
    cookie_secret: str = ""

    #: Пути файлов секретов; в тестах переопределяются на временный каталог,
    #: чтобы прогон не создавал реальные ключи в `server/data`
    secrets_key_path: Path = SECRETS_KEY_PATH
    cookie_secret_path: Path = COOKIE_SECRET_PATH

    #: Сессия администратора (подписанная cookie, itsdangerous)
    admin_cookie_name: str = ADMIN_COOKIE_NAME
    session_max_age_sec: int = 12 * 60 * 60
    cookie_secure: bool = False  # HTTPS — опция через реверс-прокси (§8.2)
    cookie_samesite: str = "lax"

    #: Пароли администраторов
    admin_password_min_length: int = 8

    #: Отдача SPA (§5.2.1): собранный `frontend\dist`
    frontend_dist_dir: Path = FRONTEND_DIST_DIR
    serve_spa: bool = True

    #: --- Канал связи с агентами (Э2) ---
    #: Схема обращения к агентам: внутри туннеля HTTP, HTTPS — через реверс-прокси (§8.2)
    agent_scheme: str = "http"
    #: Адрес сервера, который попадает в блок «что вписать в конфиг агента» (§11.2).
    #: Пусто → адрес берётся из запроса администратора (удобно при развёртывании).
    public_server_url: str = ""
    #: Health Monitor: фоновый опрос `GET /health` (§3.2, §3.3)
    health_monitor_enabled: bool = True
    #: Пауза перед первым циклом мониторинга, с (даёт серверу подняться)
    health_monitor_startup_delay_sec: float = 0.0

    #: --- Диспетчер операций (Э4, §3.2) ---
    #: Фоновая отправка задач и опрос `/check-*` (можно выключить в тестах)
    dispatcher_enabled: bool = True
    #: Пауза перед первым циклом диспетчера, с
    dispatcher_startup_delay_sec: float = 0.0
    #: Таймаут одного запроса сервера к агенту: операции живут минуты, но сам
    #: HTTP-вызов (`/run-*`, `/check-*`) должен отвечать быстро (§5.1.1)
    agent_request_timeout_sec: float = 15.0

    #: Логи сервера
    log_level: str = "INFO"
    log_file: Path = LOG_DIR / "server.log"
    log_rotate_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5

    #: Уведомления пользователям (проксируются в `commands.params` без пароля)
    command_log_excerpt_limit: int = 200 * 1024

    #: --- Картинки уведомлений (Э9, §4.6) ---
    #: Каталог файлов: в теле картинка адресуется меткой `magaz-image:<имя>`,
    #: значит файл должен переживать перезапуск и попадать в резервную копию
    notification_images_dir: Path = NOTIFICATION_IMAGES_DIR

    def model_post_init(self, __context: object) -> None:  # noqa: D105 - pydantic hook
        if not self.database_url:
            self.database_url = default_database_url()

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def ensure_directories(self) -> None:
        """Создать рабочие каталоги сервера (вызывается из `lifespan`)."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.notification_images_dir.mkdir(parents=True, exist_ok=True)
        if self.is_sqlite:
            db_path = self.database_url.replace("sqlite:///", "", 1)
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Единственный экземпляр настроек на процесс."""
    return Settings()
