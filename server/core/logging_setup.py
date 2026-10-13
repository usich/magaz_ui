"""Логирование сервера (§14.4: ротация логов).

Инвариант I-12: собственная настройка только в точке входа, глобальную
конфигурацию `logging` правим ровно один раз — здесь, из `lifespan`.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from server.config import Settings

__all__ = ["configure_logging"]

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"


def configure_logging(settings: Settings) -> None:
    """Настроить консоль + файл с ротацией; повторный вызов не дублирует хендлеры."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    if getattr(root, "_magaz_configured", False):
        return

    formatter = logging.Formatter(LOG_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    log_path = Path(settings.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.log_rotate_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # uvicorn.access шумит при polling-е SPA — оставляем только предупреждения
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    root._magaz_configured = True  # type: ignore[attr-defined]
