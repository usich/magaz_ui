"""Тесты форматированного тела и картинок уведомлений (§4.6, §11.5) — Э9.

Проверяется то, за что отвечает сервер:

* HTML из редактора проходит белый список тегов (`shared.richtext`): `script`
  не доезжает ни до SPA, ни до кассы, а текст сообщения остаётся;
* картинки лежат файлами рядом с БД, в теле — только метка `magaz-image:<имя>`,
  а касса получает список имён в полезной нагрузке `notify` (§7.4);
* счётчики вкладок (`/notifications/summary`) считают уровни и непрочитанные
  сессии — те же числа видит шапка окна кассы.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from server.config import Settings
from server.db.models import Notification, Store
from server.services import notification_service

NOTIFICATIONS_URL = "/api/v1/admin/notifications"
SUMMARY_URL = "/api/v1/admin/notifications/summary"
AUDIT_URL = "/api/v1/admin/audit"
LOGIN_URL = "/api/v1/admin/auth/login"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "magaz-test-password"

#: Настоящий PNG 1×1 — сервер проверяет тип по «магическим» байтам, а не по имени
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def login(client: TestClient) -> TestClient:
    """Войти в дашборд — фабрика `make_client` отдаёт клиент без сессии."""
    response = client.post(
        LOGIN_URL, json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return client


@pytest.fixture(autouse=True)
def _target_store(store: Store) -> Store:
    """Магазин-получатель для рассылки.

    Без активного магазина `POST /notifications` законно отвечает `422`
    («нет получателей»), поэтому фикстура нужна каждому тесту модуля, а не
    только тем, где магазин упоминается явно.
    """
    return store


def create_notification(client: TestClient, **overrides: object) -> dict:
    """Создать уведомление через API — так же, как это делает админ (§11.5)."""
    payload: dict = {
        "title": "Инвентаризация",
        "body": "Пересчитайте витрину до 18:00",
        "severity": "important",
    }
    payload.update(overrides)
    response = client.post(NOTIFICATIONS_URL, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def upload_image(
    client: TestClient,
    notification_id: int,
    *,
    name: str = "shema.png",
    data: bytes = PNG_1PX,
):
    """Отправить картинку так, как это делает редактор админки: base64 (§11.5)."""
    return client.post(
        f"{NOTIFICATIONS_URL}/{notification_id}/images",
        json={"name": name, "data_base64": base64.b64encode(data).decode("ascii")},
    )


class TestRichBody:
    """Тело с разметкой: белый список тегов и понятные ошибки (§4.6, Э9)."""

    def test_html_body_is_sanitized(self, auth_client: TestClient) -> None:
        created = create_notification(
            auth_client,
            body_format="html",
            body='<p>Обновление <b>важно</b></p><script>alert("x")</script>',
        )

        assert created["notification"]["body_format"] == "html"
        assert created["notification"]["body"] == "<p>Обновление <b>важно</b></p>"

    def test_empty_markup_is_rejected(self, auth_client: TestClient) -> None:
        response = auth_client.post(
            NOTIFICATIONS_URL,
            json={"title": "Пусто", "body": "<p> </p>", "body_format": "html"},
        )

        assert response.status_code == 422
        assert "нет текста" in response.text

    def test_unknown_format_is_rejected(self, auth_client: TestClient) -> None:
        response = auth_client.post(
            NOTIFICATIONS_URL,
            json={"title": "Формат", "body": "текст", "body_format": "markdown"},
        )

        assert response.status_code == 422
        assert "формат тела" in response.text

    def test_plain_body_is_not_parsed(self, auth_client: TestClient) -> None:
        created = create_notification(auth_client, body="Обычный <b>текст</b>")

        assert created["notification"]["body_format"] == "plain"
        assert created["notification"]["body"] == "Обычный <b>текст</b>"


class TestNotificationImages:
    """Картинки тела: файл на диске, метка в теле, имя — в нагрузке кассы (§7.4)."""

    def test_upload_and_download(self, auth_client: TestClient, settings: Settings) -> None:
        created = create_notification(auth_client)
        notification_id = created["notification"]["id"]

        uploaded = upload_image(auth_client, notification_id)
        assert uploaded.status_code == 201, uploaded.text
        body = uploaded.json()
        assert body["content_type"] == "image/png"
        assert body["size_bytes"] == len(PNG_1PX)
        assert body["url"].endswith(f"/notifications/{notification_id}/images/shema.png")

        on_disk = settings.notification_images_dir / str(notification_id) / "shema.png"
        assert on_disk.read_bytes() == PNG_1PX, "файл лежит рядом с БД, а не в теле"

        downloaded = auth_client.get(body["url"])
        assert downloaded.status_code == 200
        assert downloaded.content == PNG_1PX
        assert downloaded.headers["content-type"] == "image/png"

    def test_data_url_from_editor_is_accepted(self, auth_client: TestClient) -> None:
        created = create_notification(auth_client)
        notification_id = created["notification"]["id"]
        data_url = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode("ascii")

        uploaded = auth_client.post(
            f"{NOTIFICATIONS_URL}/{notification_id}/images",
            json={"name": "iz-bufera.png", "data_base64": data_url},
        )

        assert uploaded.status_code == 201, uploaded.text

    def test_not_an_image_is_rejected(self, auth_client: TestClient) -> None:
        created = create_notification(auth_client)
        notification_id = created["notification"]["id"]

        response = upload_image(
            auth_client, notification_id, name="secret.png", data=b"not an image"
        )

        assert response.status_code == 422
        assert "PNG, JPEG, GIF или WebP" in response.text

    def test_broken_base64_is_rejected(self, auth_client: TestClient) -> None:
        created = create_notification(auth_client)
        notification_id = created["notification"]["id"]

        response = auth_client.post(
            f"{NOTIFICATIONS_URL}/{notification_id}/images",
            json={"name": "a.png", "data_base64": "не base64"},
        )

        assert response.status_code == 422

    def test_unsafe_name_is_rejected(self, auth_client: TestClient) -> None:
        created = create_notification(auth_client)
        notification_id = created["notification"]["id"]

        response = upload_image(auth_client, notification_id, name="../secrets.key")

        assert response.status_code == 422
        assert "Имя картинки" in response.text

    def test_same_name_twice_is_rejected(self, auth_client: TestClient) -> None:
        created = create_notification(auth_client)
        notification_id = created["notification"]["id"]

        assert upload_image(auth_client, notification_id, name="dubl.png").status_code == 201
        again = upload_image(auth_client, notification_id, name="dubl.png")

        assert again.status_code == 422
        assert "уже есть" in again.text

    def test_unknown_image_is_404(self, auth_client: TestClient) -> None:
        created = create_notification(auth_client)
        notification_id = created["notification"]["id"]

        response = auth_client.get(f"{NOTIFICATIONS_URL}/{notification_id}/images/nope.png")

        assert response.status_code == 404

    def test_image_name_and_flags_travel_to_cash_register(
        self, auth_client: TestClient, session: Session, store: Store
    ) -> None:
        """Касса получает разметку, имена картинок и «удалять после прочтения».

        Картинки уезжают вместе с уведомлением: сервер не отдаёт кассе тело с
        меткой, для которой файла нет, — доставка и картинки идут одним запросом
        редактора (§4.6, §7.4).
        """
        created = create_notification(
            auth_client,
            body_format="html",
            body='<p>Схема зала</p><img src="magaz-image:shema.png">',
            delete_after_read=True,
            images=[
                {
                    "name": "shema.png",
                    "data_base64": base64.b64encode(PNG_1PX).decode("ascii"),
                }
            ],
        )
        notification_id = created["notification"]["id"]

        notification = session.get(Notification, notification_id)
        payload = notification_service.push_payload(session, store, notification)

        assert payload.body_format == "html"
        assert payload.delete_after_read is True
        assert payload.images == ["shema.png"]
        assert "magaz-image:shema.png" in payload.body

    def test_list_shows_image_names(self, auth_client: TestClient) -> None:
        created = create_notification(auth_client)
        notification_id = created["notification"]["id"]
        upload_image(auth_client, notification_id, name="karta.png")

        listed = auth_client.get(NOTIFICATIONS_URL).json()
        row = next(item for item in listed["items"] if item["id"] == notification_id)

        assert row["images"] == ["karta.png"]
        assert listed["severity_counts"]["important"] >= 1



class TestSummaryCounters:
    """Счётчики вкладок: уровни, состояния и непрочитанные сессии (§11.5, Э9)."""

    def test_counts_by_severity_and_state(self, auth_client: TestClient) -> None:
        create_notification(auth_client, title="Инфо", severity="info", body="Раз")
        create_notification(auth_client, title="Важно", severity="important", body="Два")

        summary = auth_client.get(SUMMARY_URL).json()

        assert summary["total"] == 2
        assert summary["by_severity"]["info"] == 1
        assert summary["by_severity"]["important"] == 1
        assert summary["by_severity"]["critical"] == 0
        assert summary["by_state"]["active"] == 2
        assert summary["unread_receipts"]["important"] == 0, "ещё не доставлено — ждать нечего"

    def test_summary_requires_authentication(self, client: TestClient) -> None:
        assert client.get(SUMMARY_URL).status_code == 401

    def test_image_endpoints_require_authentication(self, client: TestClient) -> None:
        assert client.post(f"{NOTIFICATIONS_URL}/1/images", json={}).status_code == 401
        assert client.get(f"{NOTIFICATIONS_URL}/1/images/a.png").status_code == 401


def test_audit_records_image_upload(auth_client: TestClient) -> None:
    """Картинка — изменение уведомления, поэтому событие видно в журнале (§4.10)."""
    created = create_notification(auth_client)
    notification_id = created["notification"]["id"]
    upload_image(auth_client, notification_id, name="audit.png")

    response = auth_client.get(AUDIT_URL, params={"limit": 20})
    actions = [item["action"] for item in response.json()]
    details = json.dumps(response.json(), ensure_ascii=False)

    assert "notification_image_add" in actions
    assert "audit.png" in details



class TestImagesOnCreate:
    """Картинки приезжают вместе с созданием: файлы есть до первой доставки (§7.4).

    Редактор админки отправляет картинки одним запросом с уведомлением. Это не
    удобство, а требование: диспетчер может отправить задачу `notify` сразу после
    создания, и касса не должна получить метку `magaz-image:<имя>` без файла.
    """

    def test_images_are_stored_with_creation(
        self, auth_client: TestClient, settings: Settings
    ) -> None:
        created = create_notification(
            auth_client,
            body_format="html",
            body='<p>Схема зала</p><img src="magaz-image:shema.png">',
            images=[
                {
                    "name": "shema.png",
                    "data_base64": base64.b64encode(PNG_1PX).decode("ascii"),
                }
            ],
        )

        notification_id = created["notification"]["id"]
        on_disk = settings.notification_images_dir / str(notification_id) / "shema.png"

        assert created["notification"]["images"] == ["shema.png"]
        assert "картинок 1" in created["message"]
        assert on_disk.read_bytes() == PNG_1PX
        served = auth_client.get(f"{NOTIFICATIONS_URL}/{notification_id}/images/shema.png")
        assert served.status_code == 200
        assert served.content == PNG_1PX

    def test_data_url_from_editor_is_accepted(self, auth_client: TestClient) -> None:
        data_url = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode("ascii")
        created = create_notification(
            auth_client,
            body_format="html",
            body='<img src="magaz-image:new.png">',
            images=[{"name": "new.png", "data_base64": data_url}],
        )

        assert created["notification"]["images"] == ["new.png"]

    def test_marker_without_image_is_dropped(self, auth_client: TestClient) -> None:
        """Метка без файла нарисовалась бы «сломанной» картинкой — её выбрасываем."""
        created = create_notification(
            auth_client,
            body_format="html",
            body='<p>Текст</p><img src="magaz-image:est.png"><img src="magaz-image:net.png">',
            images=[
                {"name": "est.png", "data_base64": base64.b64encode(PNG_1PX).decode("ascii")}
            ],
        )
        body = created["notification"]["body"]

        assert created["notification"]["images"] == ["est.png"]
        assert "est.png" in body
        assert "net.png" not in body
        assert "Текст" in body

    def test_body_without_images_drops_all_markers(self, auth_client: TestClient) -> None:
        created = create_notification(
            auth_client,
            body_format="html",
            body='<p>Только текст</p><img src="magaz-image:zabytaya.png">',
        )

        assert created["notification"]["images"] == []
        assert "magaz-image" not in created["notification"]["body"]

    def test_broken_base64_is_rejected_on_create(self, auth_client: TestClient) -> None:
        response = auth_client.post(
            NOTIFICATIONS_URL,
            json={
                "title": "Мусор",
                "body": "<p>Текст</p>",
                "body_format": "html",
                "images": [{"name": "a.png", "data_base64": "не base64"}],
            },
        )

        assert response.status_code == 422
        assert "base64" in response.text

    def test_not_an_image_is_rejected_on_create(self, auth_client: TestClient) -> None:
        response = auth_client.post(
            NOTIFICATIONS_URL,
            json={
                "title": "Не картинка",
                "body": "<p>Текст</p>",
                "body_format": "html",
                "images": [
                    {
                        "name": "a.png",
                        "data_base64": base64.b64encode(b"<html>").decode("ascii"),
                    }
                ],
            },
        )

        assert response.status_code == 422
        assert "PNG" in response.text

    def test_too_many_images_are_rejected_on_create(self, auth_client: TestClient) -> None:
        inline = base64.b64encode(PNG_1PX).decode("ascii")
        response = auth_client.post(
            NOTIFICATIONS_URL,
            json={
                "title": "Много картинок",
                "body": "<p>Текст</p>",
                "body_format": "html",
                "images": [{"name": f"kadr-{index}.png", "data_base64": inline} for index in range(11)],
            },
        )

        assert response.status_code == 422
        assert "картинок" in response.text

    def test_same_name_twice_is_rejected_on_create(self, auth_client: TestClient) -> None:
        inline = base64.b64encode(PNG_1PX).decode("ascii")
        response = auth_client.post(
            NOTIFICATIONS_URL,
            json={
                "title": "Дубль",
                "body": "<p>Текст</p>",
                "body_format": "html",
                "images": [
                    {"name": "a.png", "data_base64": inline},
                    {"name": "a.png", "data_base64": inline},
                ],
            },
        )

        assert response.status_code == 422
        assert "уже есть" in response.text

    def test_list_shows_images_and_flags(self, auth_client: TestClient) -> None:
        create_notification(
            auth_client,
            body_format="html",
            body='<img src="magaz-image:vkhod.png">',
            delete_after_read=True,
            images=[
                {"name": "vkhod.png", "data_base64": base64.b64encode(PNG_1PX).decode("ascii")}
            ],
        )

        row = auth_client.get(NOTIFICATIONS_URL).json()["items"][0]

        assert row["images"] == ["vkhod.png"]
        assert row["body_format"] == "html"
        assert row["delete_after_read"] is True


AGENT_IMAGES_URL = "/api/v1/agent/notifications"


class TestAgentImageEndpoint:
    """Касса скачивает картинку сама: токен агента и только своя адресация (§7.4, Э9).

    Окно уведомления на кассе показывает картинки из локального кэша, а скачивает их
    агент этим запросом — один раз, пока сеть есть (§7.4).
    """

    def test_agent_downloads_image(self, auth_client: TestClient, agent_headers: dict) -> None:
        created = create_notification(
            auth_client,
            body_format="html",
            body='<img src="magaz-image:shema.png">',
            images=[
                {"name": "shema.png", "data_base64": base64.b64encode(PNG_1PX).decode("ascii")}
            ],
        )
        notification_id = created["notification"]["id"]

        response = auth_client.get(
            f"{AGENT_IMAGES_URL}/{notification_id}/images/shema.png", headers=agent_headers
        )

        assert response.status_code == 200, response.text
        assert response.content == PNG_1PX
        assert response.headers["content-type"] == "image/png"

    def test_without_agent_token_is_401(self, client: TestClient, auth_client: TestClient) -> None:
        created = create_notification(auth_client)

        response = client.get(f"{AGENT_IMAGES_URL}/{created['notification']['id']}/images/nope.png")

        assert response.status_code == 401

    def test_foreign_store_is_404(
        self, auth_client: TestClient, agent_headers: dict, session: Session
    ) -> None:
        """Картинка уведомления, адресованного другому магазину, не отдаётся."""
        other = Store(
            name="Магазин №9",
            ip_address="10.20.30.41",
            cluster_name="srv-1c",
            database_name="trade",
            version_1c="8.3.24.1548",
            user_name_1c="Администратор",
            agent_token="other-store-token",
        )
        session.add(other)
        session.commit()
        created = create_notification(
            auth_client,
            target_type="stores",
            store_ids=[other.id],
            body_format="html",
            body='<img src="magaz-image:chuzhaya.png">',
            images=[
                {"name": "chuzhaya.png", "data_base64": base64.b64encode(PNG_1PX).decode("ascii")}
            ],
        )

        response = auth_client.get(
            f"{AGENT_IMAGES_URL}/{created['notification']['id']}/images/chuzhaya.png",
            headers=agent_headers,
        )

        assert response.status_code == 404
        assert "не отправлялось" in response.text

    def test_unknown_image_is_404(self, auth_client: TestClient, agent_headers: dict) -> None:
        created = create_notification(auth_client)

        response = auth_client.get(
            f"{AGENT_IMAGES_URL}/{created['notification']['id']}/images/nope.png",
            headers=agent_headers,
        )

        assert response.status_code == 404


