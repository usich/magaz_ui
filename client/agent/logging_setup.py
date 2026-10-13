"""Логирование агента (§7.1, §14.4: «ротация логов»).

Инвариант I-12: собственная настройка только в точке входа (`__main__`), глобальную
конфигурацию `logging` агент правит ровно один раз — здесь.

Файл лога: `C:\\ProgramData\\MagazAgent\\logs\\agent.log` (переопределяется
переменной `MAGAZ_AGENT_LOG_DIR`). Если каталог недоступен для записи (например,
на машине разработки), остаётся консольный вывод — агент из-за логов не падает.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from client.agent.config import AgentConfig

__all__ = ["AGENT_LOG_FILE_NAME", "configure_logging"]

#: Имя файла лога службы (§7.1)
AGENT_LOG_FILE_NAME = "agent.log"
LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"


def configure_logging(
    config: AgentConfig,
    *,
    console: bool = False,
    level: str = "INFO",
    log_file: Path | None = None,
) -> Path | None:
    """Настроить консольный вывод и файл с ротацией; вернуть путь файла лога."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric_level)

    if getattr(root, "_magaz_agent_configured", False):
        return None

    formatter = logging.Formatter(LOG_FORMAT)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)

    target = log_file or (config.log_dir / AGENT_LOG_FILE_NAME)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            target, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover - нет прав на C:\ProgramData
        logger = logging.getLogger(__name__)
        logger.warning("Не удалось открыть лог %s (%s): работаем только в консоли", target, exc)
        if not console:
            stream = logging.StreamHandler()
            stream.setFormatter(formatter)
            root.addHandler(stream)
        target = None

    root._magaz_agent_configured = True  # type: ignore[attr-defined]
    return target
