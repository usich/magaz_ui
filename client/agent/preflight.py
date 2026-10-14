"""Предполётная проверка кассы перед приёмкой (Э7, §12).

Запуск на кассе:

    magaz-agent.exe preflight
    python -m client.agent preflight --json

Проверка **ничего не меняет** и не требует запущенной службы: она читает
`agent-config.json`, смотрит файловую систему и одним запросом проверяет, виден
ли сервер с кассы. Именно эти три вещи ломаются на стенде чаще всего: не тот
токен, нет `1cv8.exe` в указанной версии, туннель до сервера не поднят.

Секреты в отчёт не попадают (I-7): токен и пароль 1С показываются как «есть/нет».
"""

from __future__ import annotations

import shutil
import socket
from pathlib import Path

from client.agent.config import AgentConfig, default_config_path, load_config
from shared.preflight import (
    CHECK_FAIL,
    CHECK_OK,
    CHECK_WARN,
    CheckResult,
    PreflightReport,
    check,
    console_safe,
)
from shared.version import APP_VERSION

__all__ = [
    "ALLOWED_PORTS",
    "MSG_EXE",
    "ONE_C_EXE",
    "SERVER_HEALTH_PATH",
    "render_preflight",
    "run_preflight",
]

#: Имя исполняемого файла платформы 1С (§7.1)
ONE_C_EXE = "1cv8.exe"
#: Дублирующий канал уведомлений в сессии пользователя (§7.4)
MSG_EXE = "msg.exe"
#: Точка «сервер жив» на стороне сервера (`server/main.py`, §3.2)
SERVER_HEALTH_PATH = "/api/v1/health"
#: Порт агента слушает `0.0.0.0` (§7.1); 127.0.0.1 проверяем отдельно —
#: его занимает и exe, запущенный в сессии пользователя
ALLOWED_PORTS = ("0.0.0.0", "127.0.0.1")


def _writable(directory: Path) -> str | None:
    """Проверить запись в каталог: `None` — можно писать, иначе текст ошибки."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".preflight.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return str(exc)
    return None


def _check_config_file(config_path: Path, config: AgentConfig | None, error: str) -> CheckResult:
    """Конфиг прочитан и заполнен тем, без чего агент бесполезен (§7.2)."""
    if config is None:
        return check(
            "Конфиг агента",
            CHECK_FAIL,
            error,
            f"возьмите шаблон в карточке магазина и положите его в {config_path} (§11.2)",
        )
    empty = []
    if not config.store_id:
        empty.append("store_id")
    if not config.server_url:
        empty.append("server_url")
    if not config.agent_token:
        empty.append("agent_token")
    if empty:
        return check(
            "Конфиг агента",
            CHECK_FAIL,
            f"{config_path}: не заполнено: {', '.join(empty)}",
            "скопируйте значения из карточки магазина (§11.2)",
        )
    return check(
        "Конфиг агента",
        CHECK_OK,
        f"{config_path} (магазин id={config.store_id}, токен есть)",
    )


def _check_server(config: AgentConfig, timeout: float = 5.0) -> CheckResult:
    """Виден ли сервер с кассы: `GET /api/v1/health` (§3.2)."""
    import httpx

    url = f"{config.server_url.rstrip('/')}{SERVER_HEALTH_PATH}"
    try:
        response = httpx.get(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - показываем причину как есть
        return check(
            "Сервер доступен",
            CHECK_FAIL,
            f"{url}: {exc}",
            "проверьте туннель/сеть до сервера и адрес в конфиге (§7.2)",
        )
    if response.status_code != 200:
        return check(
            "Сервер доступен",
            CHECK_FAIL,
            f"{url}: HTTP {response.status_code}",
            "сервер отвечает, но не так, как ждёт агент: смотрите лог сервера (§9.2)",
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    version = payload.get("app_version", "") if isinstance(payload, dict) else ""
    detail = url if not version else f"{url} (сервер {version}, {payload.get('status', '?')})"
    return check("Сервер доступен", CHECK_OK, detail)


def _check_one_c(config: AgentConfig) -> CheckResult:
    """Платформа 1С на кассе: каталог версий и хотя бы один `1cv8.exe` (§7.1, I-14).

    Версию агент получает из карточки магазина (`store.version_1c`) — здесь
    проверяем только то, что каталог существует и в нём есть рабочие версии:
    на стенде «версия в магазине не та, что на кассе» — типичная находка.
    """
    base = Path(config.base_1c_path)
    if not base.is_dir():
        return check(
            "Платформа 1С",
            CHECK_FAIL,
            f"нет каталога {base}",
            "укажите base_1c_path в agent-config.json (§7.2)",
        )
    versions = sorted(
        child.name
        for child in base.iterdir()
        if child.is_dir() and (child / "bin" / ONE_C_EXE).is_file()
    )
    if not versions:
        return check(
            "Платформа 1С",
            CHECK_FAIL,
            f"{base}: не найден {ONE_C_EXE} ни в одной версии",
            "проверьте, что 1С установлена и путь ведёт к каталогу версий (§7.1)",
        )
    return check("Платформа 1С", CHECK_OK, f"{base} — версии: {', '.join(versions)}")


def _check_msg() -> CheckResult:
    """Дублирующий канал `msg.exe` (§7.4, R2): недоступен — не блокирует работу."""
    found = shutil.which("msg")
    if found:
        return check("Дублирующий канал msg.exe", CHECK_OK, found)
    return check(
        "Дублирующий канал msg.exe",
        CHECK_WARN,
        "не найден в PATH",
        "уведомления покажет TrayAgent; на Windows Home и при политиках msg.exe "
        "недоступен — это штатно (R2)",
    )


def _check_ports(config: AgentConfig) -> CheckResult:
    """Порт агента (§7.1): занят — скорее всего служба уже запущена."""
    busy: list[str] = []
    for host in ALLOWED_PORTS:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            sock.bind((host, config.port))
        except OSError:
            busy.append(host)
        finally:
            sock.close()
    if not busy:
        return check("Порт агента", CHECK_OK, f"{config.port} свободен на {ALLOWED_PORTS[0]}")
    return check(
        "Порт агента",
        CHECK_WARN,
        f"{config.port} занят на {', '.join(busy)}",
        "для приёмки это ожидаемо, если служба уже работает; иначе ищите чужой процесс",
    )


def _check_directories(config: AgentConfig) -> CheckResult:
    """Рабочая папка и каталог логов: без записи в них агент не работает (§7.1, §9.2)."""
    problems: list[str] = []
    for directory in (config.work_path, config.log_dir):
        error = _writable(Path(directory))
        if error:
            problems.append(f"{directory}: {error}")
    if problems:
        return check(
            "Рабочая папка и логи",
            CHECK_FAIL,
            "; ".join(problems),
            "служба работает под LocalSystem — дайте ему права на эти каталоги (§14.2)",
        )
    return check("Рабочая папка и логи", CHECK_OK, f"{config.work_path}; {config.log_dir}")


def _check_credentials(config: AgentConfig) -> CheckResult:
    """Учётные данные 1С (§7.2, П-1): без них операции с 1С не пойдут.

    Пароль проверяется по факту: `password_enc != None`. Самого значения в
    отчёте нет (I-7) — иначе предполётная проверка стала бы утечкой.
    """
    user = config.one_c_profile.user_name
    has_password = bool(config.one_c_profile.password_enc)
    if user and has_password:
        return check(
            "Учётные данные 1С",
            CHECK_OK,
            f"пользователь {user!r}, пароль доставлен (версия {config.credentials_version})",
        )
    missing = []
    if not user:
        missing.append("логин не задан в карточке магазина")
    if not has_password:
        missing.append("пароль не доставлен")
    return check(
        "Учётные данные 1С",
        CHECK_WARN,
        "; ".join(missing),
        "заполните логин и пароль в карточке магазина: агент получит их push-ом "
        "POST /credentials (§7.2, П-1)",
    )


def run_preflight(
    config: AgentConfig | None = None,
    *,
    config_path: str | Path | None = None,
    check_server: bool = True,
) -> PreflightReport:
    """Собрать отчёт предполётной проверки кассы.

    `check_server=False` — не ходить на сервер (нужно тестам и случаю «сервер ещё
    не поставлен, конфиг уже привезли»).
    """
    resolved = Path(config_path) if config_path else default_config_path()
    report = PreflightReport(title=f"Предполётная проверка кассы MagazAgent {APP_VERSION}")

    error = ""
    if config is None:
        try:
            config = load_config(resolved)
        except Exception as exc:  # noqa: BLE001 - отсутствие файла и битый JSON
            error = str(exc)
    config_result = _check_config_file(resolved, config, error)
    report.add(config_result)
    if config is None:
        return report

    if check_server:
        report.add(_check_server(config))
    report.add(_check_one_c(config))
    report.add(_check_msg())
    report.add(_check_ports(config))
    report.add(_check_directories(config))
    report.add(_check_credentials(config))
    report.add(check("Версия MagazAgent", CHECK_OK, APP_VERSION))
    return report


def render_preflight(report: PreflightReport, *, as_json: bool = False) -> str:
    """Вывод отчёта: обычный текст для консоли или JSON для протокола.

    Текстовый вывод проходит через `console_safe`: на кассе `cmd` работает в cp1251,
    и без этого `print` падает на символах вроде `→` (§14.2).
    """
    if as_json:
        import json

        return json.dumps(report.as_dict(), ensure_ascii=False, indent=2)
    return console_safe(report.render())


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    raise SystemExit(run_preflight().exit_code)
