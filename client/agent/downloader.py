"""Скачивание пакета обновления по публичной ссылке Яндекс.Диска (FR-1, ФН2).

Перенос v2 1:1: сначала запрос к `cloud-api.yandex.net/v1/disk/public/resources/download`,
затем скачивание по выданной ссылке. Единственное содержательное отличие —
имя файла берётся через `shared.paths.safe_basename` (I-2): ссылка не может
записать файл вне рабочей папки агента (`..\\..\\evil.exe` отсекается).

Рабочая папка приходит аргументом (`work_dir`, I-14), httpx-клиент можно
передать снаружи — в тестах используется `httpx.MockTransport` (§13).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from shared.paths import safe_basename
from shared.timeutil import to_iso_z, utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "DOWNLOAD_TIMEOUT_SEC",
    "YANDEX_DOWNLOAD_API",
    "LAST_DOWNLOAD_FILE",
    "download_file_from_yandex_disk",
    "file_name_from_yandex_payload",
]

#: Публичный API Яндекс.Диска: получение ссылки на скачивание по публичной ссылке
YANDEX_DOWNLOAD_API = "https://cloud-api.yandex.net/v1/disk/public/resources/download"
#: Таймаут скачивания пакета обновления (файлы бывают десятки мегабайт)
DOWNLOAD_TIMEOUT_SEC = 120.0
#: Мета-файл: имя последнего скачанного пакета. Сервер читает его через
#: `/check-files` (`downloaded`), чтобы знать реальное имя файла, когда ссылка
#: не несёт имени (§11.3, FR-1).
LAST_DOWNLOAD_FILE = "last-download.json"


def _yandex_filename_from_href(href: str) -> str | None:
    """Имя файла из прямой ссылки Яндекс.Диска — query-параметр `filename` (I-2).

    Публичный API (`/public/resources/download`) для ссылок вида `/d/<id>` не
    всегда возвращает поле `name`, но кладёт реальное имя файла в `href` как
    параметр `filename=<urlencoded>` (наблюдение на кассе, «magaz UI»). Берём
    его как запасной вариант имени — то же имя, что Диск отдаёт при скачивании.
    """
    try:
        query = parse_qs(urlsplit(href).query, keep_blank_values=True)
        names = query.get("filename") or []
        if not names:
            return None
        return safe_basename(str(names[0]))
    except ValueError:
        return None


def file_name_from_yandex_payload(payload: dict, href: str | None = None) -> str | None:
    """Имя файла из ответа Яндекс.Диска — только basename (I-2).

    Сначала поле `name` (как раньше), затем — запасной вариант: имя из прямой
    ссылки `href` (query-параметр `filename`), потому что Диск не всегда
    возвращает `name`. Некорректное или пустое имя означает, что сохранять файл
    некуда: возвращаем `None`, а вызывающая сторона пишет ошибку в лог.
    """
    try:
        return safe_basename(str(payload.get("name", "")))
    except ValueError:
        pass
    if href:
        from_href = _yandex_filename_from_href(href)
        if from_href:
            return from_href
    logger.error("Яндекс.Диск не вернул допустимое имя файла (ни `name`, ни `filename` из `href`)")
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
    # trust_env=False: та же «ловушка прокси», что закрыта в AgentClient / devices /
    # notifications / tray.api (см. README, отклонение №2 и TestProxyIsIgnored). Без
    # этого httpx берёт системный прокси (туннель) из окружения, и реальное скачивание
    # с Яндекс.Диска не доводится до конца: download_status="failed".
    http = client or httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, trust_env=False
    )
    try:
        _write_last_download(work_dir, {"status": "downloading", "filename": None})
        response = await http.get(YANDEX_DOWNLOAD_API, params={"public_key": public_key})
        response.raise_for_status()
        payload = response.json()

        download_url = payload.get("href")
        file_name = file_name_from_yandex_payload(payload, href=download_url)
        if not download_url or not file_name:
            _write_last_download(
                work_dir,
                {"status": "failed", "filename": None, "error": "ссылка не получена"},
            )
            logger.error("Яндекс.Диск не вернул ссылку на файл: %s", public_key)
            return None

        download = await http.get(download_url)
        download.raise_for_status()

        target = work_dir / file_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(download.content)
        logger.info("Файл %s загружен (%s байт)", target, len(download.content))
        _write_last_download(
            work_dir,
            {"status": "done", "filename": file_name, "downloaded_at": to_iso_z(utcnow())},
        )
        return target
    except httpx.HTTPStatusError as exc:
        _write_last_download(
            work_dir,
            {"status": "failed", "filename": None, "error": f"HTTP {exc.response.status_code}"},
        )
        logger.error("Ошибка HTTP при загрузке файла: %s", exc)
        return None
    except httpx.HTTPError as exc:
        _write_last_download(
            work_dir,
            {"status": "failed", "filename": None, "error": "сетевой сбой"},
        )
        logger.error("Ошибка сети при загрузке файла: %s", exc)
        return None
    except OSError as exc:
        _write_last_download(
            work_dir,
            {"status": "failed", "filename": None, "error": str(exc)},
        )
        logger.error("Не удалось сохранить файл в %s: %s", work_dir, exc)
        return None
    finally:
        if owns_client:
            await http.aclose()


def _write_last_download(work_dir: Path, data: dict) -> None:
    """Записать мета-файл последнего скачивания (ошибки не выбрасываются)."""
    try:
        meta = work_dir / LAST_DOWNLOAD_FILE
        meta.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.error("Не удалось записать %s: %s", LAST_DOWNLOAD_FILE, exc)
