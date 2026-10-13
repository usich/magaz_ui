"""Общий пакет «Magaz UI»: версия, Enum-ы статусов, схемы команд и уведомлений.

Используется и сервером (`server/`), и агентом (`client/agent/`), поэтому здесь
не должно быть зависимостей от FastAPI/SQLAlchemy/Windows API.
"""

from .version import APP_NAME, APP_VERSION

__all__ = ["APP_NAME", "APP_VERSION"]
