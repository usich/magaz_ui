"""Отдача SPA (§5.2.1): собранная статика `frontend\\dist` + history-fallback.

Правило маршрутизации простое и одно на весь сервер:

* всё, что начинается с `api/`, обслуживают JSON-роутеры — fallback молчит;
* существующий файл из `dist` отдаётся как есть (js/css/иконки);
* любой другой GET возвращает `index.html`, поэтому прямые ссылки вида
  `/stores/12` работают после перезагрузки страницы (Vue Router history mode).

Если SPA ещё не собрана (`npm run build` не выполнялся), возвращается понятная
HTML-страница 503 с подсказкой — это состояние «каркас развёрнут, фронт не собран».
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import FileResponse, HTMLResponse

from server.config import API_PREFIX, Settings

logger = logging.getLogger(__name__)

__all__ = ["INDEX_FILE_NAME", "build_spa_router", "render_not_built_page"]

INDEX_FILE_NAME = "index.html"


def render_not_built_page(dist_dir: Path, app_version: str) -> str:
    """HTML-заглушка: сервер жив, но `frontend\\dist` отсутствует."""
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Magaz UI — интерфейс не собран</title>
  <style>
    body {{ font-family: "Segoe UI", Arial, sans-serif; background: #0f172a; color: #e2e8f0;
           display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
    .card {{ max-width: 640px; background: #1e293b; padding: 32px 40px; border-radius: 12px;
             box-shadow: 0 20px 40px rgba(0,0,0,.35); }}
    code {{ background: #0f172a; padding: 2px 6px; border-radius: 4px; color: #93c5fd; }}
    h1 {{ margin-top: 0; font-size: 22px; }}
    ol {{ line-height: 1.7; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Интерфейс дашборда ещё не собран</h1>
    <p>Сервер Magaz UI <strong>v{app_version}</strong> работает. JSON API доступен по
       <code>{API_PREFIX}/admin/</code>.</p>
    <p>Каталог сборки: <code>{dist_dir}</code></p>
    <ol>
      <li><code>cd frontend</code></li>
      <li><code>npm ci</code> (первый раз)</li>
      <li><code>npm run build</code></li>
      <li>обновить эту страницу</li>
    </ol>
  </div>
</body>
</html>
"""


def build_spa_router(settings: Settings) -> APIRouter:
    """Роутер-«ловушка» для SPA: файлы из `dist` и history-fallback на `index.html`."""
    router = APIRouter(tags=["spa"])
    dist_dir = Path(settings.frontend_dist_dir)
    index_file = dist_dir / INDEX_FILE_NAME
    api_prefix = API_PREFIX.strip("/")

    @router.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> Response:
        """Отдать файл из `dist` либо `index.html` (кроме путей `api/*`)."""
        normalized = (full_path or "").strip("/")
        if normalized == api_prefix or normalized.startswith(f"{api_prefix}/"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Не найдено")

        if normalized:
            candidate = dist_dir / normalized

            def _resolve_candidate() -> tuple[Path, bool]:
                # Синхронный доступ к ФС (stat/resolve); выносим в поток, чтобы не
                # блокировать event loop uvicorn на каждый GET-запрос фронта.
                try:
                    resolved = candidate.resolve()
                    inside = dist_dir.resolve() in resolved.parents
                except OSError:  # pragma: no cover - нечитаемый путь
                    return candidate, False
                return resolved, inside

            resolved, inside_dist = await asyncio.to_thread(_resolve_candidate)
            if inside_dist and await asyncio.to_thread(resolved.is_file):
                return FileResponse(resolved)

        if await asyncio.to_thread(index_file.is_file):
            return FileResponse(index_file)

        logger.warning("SPA не собрана: нет файла %s", index_file)
        return HTMLResponse(
            render_not_built_page(dist_dir, settings.app_version),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return router
