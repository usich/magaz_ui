"""Скачивание пакета обновления по публичной ссылке Яндекс.Диска (FR-1, ФН2).

Перенос v2 1:1: сначала запрос к `cloud-api.yandex.net/v1/disk/public/resources/download`,
затем скачивание по выданной ссылке. Единственное содержательное отличие —
имя файла берётся через `shared.paths.safe_basename` (I-2): ссылка не может
записать файл вне рабочей папки агента (`..\\..\\evil.exe` отсекается).

Рабочая папка приходит аргументом (`work_dir`, I-14), httpx-клиент можно
передать снаружи — в тестах используется `httpx.MockTransport` (§13).
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from shared.paths import safe_basename

logger = logging.getLogger(__name__)

__all__ = [
    "DOWNLOAD_TIMEOUT_SEC",
    "YANDEX_DOWNLOAD_API",
    "download_file_from_yandex_disk",
    "file_name_from_yandex_payload",
]

#: Публичный API Яндекс.Диска: получение ссылки на скачивание по публичной ссылке
YANDEX_DOWNLOAD_API = "https://cloud-api.yandex.net/v1/disk/public/resources/download"
#: Таймаут скачивания пакета обновления (файлы бывают десятки мегабайт)
DOWNLOAD_TIMEOUT_SEC = 120.0


def file_name_from_yandex_payload(payload: dict) -> str | None:
    """Имя файла из ответа Яндекс.Диска — только basename (I-2).

    Некорректное или пустое имя означает, что сохранять файл некуда: возвращаем
    `None`, а вызывающая сторона пишет ошибку в лог.
    """
    try:
        return safe_basename(str(payload.get("name", "")))
    except ValueError as exc:
        logger.error("Яндекс.Диск вернул недопустимое имя файла: %s", exc)
        return None


async def download_file_from_yandex_disk(
    public_key: str,
    *,
    work_dir: Path,
    client: httpx.AsyncClient | None = None,
    timeout: float = DOWNLOAD_TIMEOUT_SEC,
) -> Path | None:
    """Скачать файл по публичной ссылке в рабочую папку агента (FR-1).

    Возвращает путь к скачанному файлу либо `None`, если диск не ответил,
    не отдал ссылку или имя файла небезопасно. Ошибки не выбрасываются наружу:
    задача скачивания выполняется в фоне и её результат сервер читает из задачи.
    """
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    try:
        response = await http.get(YANDEX_DOWNLOAD_API, params={"public_key": public_key})
        response.raise_for_status()
        payload = response.json()

        download_url = payload.get("href")
        file_name = file_name_from_yandex_payload(payload)
        if not download_url or not file_name:
            logger.error("Яндекс.Диск не вернул ссылку на файл: %s", public_key)
            return None

        download = await http.get(download_url)
        download.raise_for_status()

        target = work_dir / file_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(download.content)
        logger.info("Файл %s загружен (%s байт)", target, len(download.content))
        return target
    except httpx.HTTPStatusError as exc:
        logger.error("Ошибка HTTP при загрузке файла: %s", exc)
        return None
    except httpx.HTTPError as exc:
        logger.error("Ошибка сети при загрузке файла: %s", exc)
        return None
    except OSError as exc:
        logger.error("Не удалось сохранить файл в %s: %s", work_dir, exc)
        return None
    finally:
        if owns_client:
            await http.aclose()
