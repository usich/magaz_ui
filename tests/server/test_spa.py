"""Тесты отдачи SPA (§5.2.1): статика `frontend\\dist`, history-fallback, 503 до сборки."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from server.config import Settings

INDEX_HTML = "<!doctype html><html><body>SPA OK</body></html>"
APP_JS = "console.log('magaz-ui');"


def build_stub_dist(settings: Settings) -> None:
    """Имитация результата `npm run build` (index.html + assets/app.js)."""
    dist = settings.frontend_dist_dir
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (dist / "assets" / "app.js").write_text(APP_JS, encoding="utf-8")


class TestSpaNotBuilt:
    """Каркас развёрнут, фронт ещё не собран — сервер объясняет, что делать."""

    def test_root_returns_503_with_hint(self, client: TestClient) -> None:
        response = client.get("/")

        assert response.status_code == 503
        assert "не собран" in response.text
        assert "npm run build" in response.text
        assert response.headers["content-type"].startswith("text/html")

    def test_json_api_still_works(self, client: TestClient) -> None:
        assert client.get("/api/v1/health").status_code == 200


class TestSpaServing:
    """Собранная SPA: файлы из `dist` и history-fallback на `index.html`."""

    def _client(self, app: FastAPI) -> TestClient:  # noqa: ANN401
        return TestClient(app)

    def test_index_and_assets_are_served(
        self, settings: Settings, engine: Engine, app: FastAPI  # noqa: ARG002
    ) -> None:
        build_stub_dist(settings)
        with self._client(app) as client:
            root = client.get("/")
            assert root.status_code == 200
            assert "SPA OK" in root.text

            asset = client.get("/assets/app.js")
            assert asset.status_code == 200
            assert APP_JS in asset.text

    def test_client_route_falls_back_to_index(
        self, settings: Settings, engine: Engine, app: FastAPI  # noqa: ARG002
    ) -> None:
        build_stub_dist(settings)
        with self._client(app) as client:
            for route in ("/stores", "/stores/12", "/devices", "/notifications/3"):
                response = client.get(route)
                assert response.status_code == 200, route
                assert "SPA OK" in response.text, route

    def test_unknown_api_path_is_json_404_not_spa(
        self, settings: Settings, engine: Engine, app: FastAPI  # noqa: ARG002
    ) -> None:
        build_stub_dist(settings)
        with self._client(app) as client:
            response = client.get("/api/v1/admin/definitely-not-here")

            assert response.status_code == 404
            assert response.headers["content-type"].startswith("application/json")
            assert "SPA OK" not in response.text

    def test_swagger_ui_still_available(
        self, settings: Settings, engine: Engine, app: FastAPI  # noqa: ARG002
    ) -> None:
        build_stub_dist(settings)
        with self._client(app) as client:
            assert client.get("/api/docs").status_code == 200
            assert client.get("/api/openapi.json").status_code == 200

    def test_path_traversal_outside_dist_is_blocked(
        self, settings: Settings, engine: Engine, app: FastAPI  # noqa: ARG002
    ) -> None:
        build_stub_dist(settings)
        with self._client(app) as client:
            response = client.get("/..%2f..%2frequirements.txt")

            assert response.status_code == 200
            assert "SPA OK" in response.text, "наружу за пределы dist статика не отдаётся"
            assert "fastapi" not in response.text
