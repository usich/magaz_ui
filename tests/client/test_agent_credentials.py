"""Учётные данные 1С агента: push `/credentials` и реальная расшифровка (П-1, Д-6).

Проверяется то, что в Э3 было отложено: агент принимает пароль от сервера,
хранит его зашифрованным (Fernet + ключ под DPAPI машины), подставляет в команду
1С, если сервер не прислал пароль в теле, и никогда не пишет его в лог (I-7).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from client.agent.app import create_agent_app
from client.agent.config import OneCProfile
from client.agent.credentials import CredentialCipher, OneCProfileStore, dpapi_available
from shared.schemas import CredentialsPush
from tests.client.conftest import AGENT_TOKEN, RecordingProvider, make_config

#: Пароль 1С в тестах: этим значением проверяется шифрование и отсутствие утечек
PASSWORD = "S3cret-1C-password"


def make_config_file(tmp_path: Path, **overrides) -> Path:  # noqa: ANN003
    """Реальный `agent-config.json` рядом с агентом (§7.2)."""
    data = {
        "store_id": 12,
        "store_name": "Магазин №5",
        "server_url": "http://10.10.0.1:8080",
        "agent_token": AGENT_TOKEN,
        "port": 8383,
        "base_1c_path": "C:/Program Files (x86)/1cv8",
        "one_c_profile": {
            "user_name": "Администратор",
            "password_enc": None,
            "credentials_version": 1,
        },
    }
    data.update(overrides)
    path = tmp_path / "agent-config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def make_app(
    tmp_path: Path, *, provider: RecordingProvider | None = None
) -> tuple[FastAPI, Path]:
    """Агент с реальным файлом конфига во временном каталоге."""
    config_path = make_config_file(tmp_path)
    app = create_agent_app(
        make_config(work_dir=str(tmp_path)), config_path=config_path, provider=provider
    )
    return app, config_path


class TestCredentialsEndpoint:
    """`POST /credentials` — приём учётных данных от сервера (§5.1.2, §8.3)."""

    def test_push_stores_profile_and_health_reports_it(self, tmp_path: Path) -> None:
        app, config_path = make_app(tmp_path)

        with TestClient(app) as client:
            response = client.post(
                "/credentials",
                json={
                    "user_name": "Администратор",
                    "password": PASSWORD,
                    "credentials_version": 9,
                },
                headers={"X-Agent-Token": AGENT_TOKEN},
            )
            assert response.status_code == 200, response.text
            assert response.json()["stored"] is True
            assert response.json()["credentials_version"] == 9
            assert response.json()["password_set"] is True

            health = client.get("/health", headers={"X-Agent-Token": AGENT_TOKEN}).json()
            assert health["credentials_version"] == 9, "касса докладывает новую версию (П-1)"

        saved = json.loads(config_path.read_text(encoding="utf-8"))
        assert saved["one_c_profile"]["credentials_version"] == 9
        assert saved["one_c_profile"]["user_name"] == "Администратор"
        assert saved["one_c_profile"]["password_enc"], "профиль сохранён в конфиг (§7.2)"

    def test_password_is_encrypted_at_rest(self, tmp_path: Path) -> None:
        """Пароль не лежит в открытом виде ни в конфиге, ни в файле ключа."""
        app, config_path = make_app(tmp_path)

        with TestClient(app) as client:
            client.post(
                "/credentials",
                json={
                    "user_name": "Администратор",
                    "password": PASSWORD,
                    "credentials_version": 3,
                },
                headers={"X-Agent-Token": AGENT_TOKEN},
            )

        raw = config_path.read_text(encoding="utf-8")
        assert PASSWORD not in raw
        profile = OneCProfile.model_validate(json.loads(raw)["one_c_profile"])
        cipher = CredentialCipher(tmp_path / ".credentials.key")
        assert cipher.decrypt(profile.password_enc) == PASSWORD

        key_file = tmp_path / ".credentials.key"
        assert key_file.is_file(), "ключ профиля лежит рядом с конфигом (§8.3)"
        assert PASSWORD not in key_file.read_bytes().decode("latin-1")
        if dpapi_available():
            assert key_file.read_bytes().startswith(b"dpapi:"), "на Windows ключ под DPAPI"

    def test_password_is_never_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """I-7: ни одной строки лога с паролем — ни при приёме, ни при сохранении."""
        app, _ = make_app(tmp_path)

        with caplog.at_level("DEBUG"):
            with TestClient(app) as client:
                client.post(
                    "/credentials",
                    json={
                        "user_name": "Администратор",
                        "password": PASSWORD,
                        "credentials_version": 5,
                    },
                    headers={"X-Agent-Token": AGENT_TOKEN},
                )

        assert PASSWORD not in caplog.text

    def test_empty_user_name_is_rejected(self, tmp_path: Path) -> None:
        app, _ = make_app(tmp_path)

        with TestClient(app) as client:
            response = client.post(
                "/credentials",
                json={"user_name": "", "password": PASSWORD, "credentials_version": 2},
                headers={"X-Agent-Token": AGENT_TOKEN},
            )

        assert response.status_code == 422

    def test_endpoint_requires_agent_token(self, tmp_path: Path) -> None:
        app, _ = make_app(tmp_path)

        with TestClient(app) as client:
            response = client.post(
                "/credentials",
                json={
                    "user_name": "Администратор",
                    "password": PASSWORD,
                    "credentials_version": 2,
                },
            )

        assert response.status_code == 401

    def test_empty_password_clears_profile(self, tmp_path: Path) -> None:
        """Пустой пароль — «аутентификация не нужна»: профиль очищается, версия растёт."""
        app, config_path = make_app(tmp_path)

        with TestClient(app) as client:
            client.post(
                "/credentials",
                json={
                    "user_name": "Администратор",
                    "password": PASSWORD,
                    "credentials_version": 2,
                },
                headers={"X-Agent-Token": AGENT_TOKEN},
            )
            response = client.post(
                "/credentials",
                json={"user_name": "Администратор", "password": "", "credentials_version": 3},
                headers={"X-Agent-Token": AGENT_TOKEN},
            )

        assert response.json()["password_set"] is False
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        assert saved["one_c_profile"]["password_enc"] is None


class TestProfilePassword:
    """Д-6/П-1: пароль из профиля подставляется в команду 1С, но не попадает в лог."""

    async def test_merge_takes_password_from_profile(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Сервер не присылает пароль — агент берёт его из зашифрованного профиля."""
        provider = RecordingProvider()
        config_path = make_config_file(tmp_path)
        app = create_agent_app(
            make_config(work_dir=str(tmp_path)), config_path=config_path, provider=provider
        )
        headers = {"X-Agent-Token": AGENT_TOKEN}

        with caplog.at_level("DEBUG"):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
                pushed = await client.post(
                    "/credentials",
                    json={
                        "user_name": "Администратор",
                        "password": PASSWORD,
                        "credentials_version": 4,
                    },
                    headers=headers,
                )
                assert pushed.status_code == 200

                response = await client.post(
                    "/run-merge",
                    json={
                        "server_name": "srv-1c",
                        "base_name": "trade",
                        "version_1c": "8.3.24.1548",
                        "user_name": "Администратор",
                        "file_name": "update",
                    },
                    headers=headers,
                )
                assert response.status_code == 202, response.text
                await app.state.agent_runner.wait_idle()

        assert provider.commands, "команда 1С ушла на выполнение"
        argv = provider.commands[0]
        assert f"/P{PASSWORD}" in argv, "пароль из профиля дошёл до 1С (П-1)"
        assert "/NАдминистратор" in argv
        assert PASSWORD not in caplog.text, "I-7: пароль не попал в журнал агента"

    async def test_explicit_password_from_server_wins(
        self, tmp_path: Path
    ) -> None:
        """Перенос v2: пароль в теле команды главнее профиля (§5.1.1)."""
        provider = RecordingProvider()
        app = create_agent_app(
            make_config(work_dir=str(tmp_path)), provider=provider
        )
        headers = {"X-Agent-Token": AGENT_TOKEN}

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
            response = await client.post(
                "/run-merge",
                json={
                    "server_name": "srv-1c",
                    "base_name": "trade",
                    "version_1c": "8.3.24.1548",
                    "user_name": "Администратор",
                    "password": "ИзЗапроса",
                    "file_name": "update",
                },
                headers=headers,
            )
            assert response.status_code == 202
            await app.state.agent_runner.wait_idle()

        assert "/PИзЗапроса" in provider.commands[0]


class TestCredentialCipher:
    """Шифрование профиля 1С: ключ машины, повторное использование, битые токены."""

    def test_roundtrip_and_key_reuse(self, tmp_path: Path) -> None:
        key_path = tmp_path / ".credentials.key"
        first = CredentialCipher(key_path)
        token = first.encrypt(PASSWORD)

        second = CredentialCipher(key_path)

        assert token != PASSWORD
        assert second.decrypt(token) == PASSWORD
        assert dpapi_available() or key_path.read_bytes() == first._load_key()

    def test_broken_token_is_none(self, tmp_path: Path) -> None:
        cipher = CredentialCipher(tmp_path / ".credentials.key")

        assert cipher.decrypt("не-токен") is None
        assert cipher.decrypt(None) is None

    def test_foreign_key_cannot_decrypt(self, tmp_path: Path) -> None:
        token = CredentialCipher(tmp_path / "a.key").encrypt(PASSWORD)
        other = CredentialCipher(tmp_path / "b.key")

        assert other.decrypt(token) is None, "ключ другой машины пароль не открывает"

    def test_store_without_config_file_keeps_profile_in_memory(self, tmp_path: Path) -> None:
        """Push без файла конфига (прогон `--console`) не создаёт лишних файлов."""
        store = OneCProfileStore(
            OneCProfile(), cipher=CredentialCipher(tmp_path / ".credentials.key")
        )

        profile = store.apply_push(
            CredentialsPush(user_name="Администратор", password=PASSWORD, credentials_version=6)
        )

        assert profile.credentials_version == 6
        assert store.resolve("") == PASSWORD
        assert not (tmp_path / "agent-config.json").exists()