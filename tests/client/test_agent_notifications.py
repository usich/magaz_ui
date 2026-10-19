"""Уведомления на кассе: очередь, сессии, «Отложить» и досылка (§7.4, Э5).

Ни сети, ни окон `msg.exe` здесь нет: процессы подменены провайдером (§7.5),
сервер — `httpx.MockTransport` (§13). Проверяется то, что важно на кассе:

* уведомление приходит push-ом и переживает перезапуск службы (файл
  `notifications.json`);
* учёт идёт по сессиям Windows, а «Отложить» (П-2) **не** считается
  ознакомлением: уведомление скрывается и возвращается через `snooze_min`;
* `ack` досылается на сервер, а при недоступном сервере остаётся в очереди;
* дублирующий канал `msg.exe` вызывается для `important`/`critical` и не
  трогает процессы 1С (в отличие от `/send-message`).
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import timedelta
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from client.agent import notifications
from client.agent.app import create_agent_app
from client.agent.config import AGENT_NOTIFICATIONS_FILE_NAME
from shared.schemas import NotifyRequest, SessionRef
from shared.timeutil import to_iso_z, utcnow
from tests.client.conftest import make_config

HEADERS = {"X-Agent-Token": "agent-token-for-tests"}


class StubProvider:
    """Провайдер процессов: запоминает argv и ничего не запускает (§7.5)."""

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.commands: list[list[str]] = []

    async def run_captured(  # noqa: ANN001 - интерфейс провайдера
        self, args: list[str], *, log_path: Path | None = None, timeout: float | None = None
    ) -> int | None:
        self.commands.append(list(args))
        return 0

    async def run_quiet(self, args: list[str]) -> bool:
        self.commands.append(list(args))
        return self.available


class ScriptedServer:
    """Мок сервера: принимает пакет подтверждений (§5.2.2)."""

    def __init__(self, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.batches: list[dict] = []
        self.paths: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.batches.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(self.status_code, json={"ok": True})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def build_queue(tmp_path: Path) -> notifications.NotificationQueue:
    """Очередь на временном файле — как `C:\\MagazAgent\\notifications.json` (§7.4)."""
    return notifications.NotificationQueue.load(tmp_path / AGENT_NOTIFICATIONS_FILE_NAME)


def notify_request(**overrides: object) -> NotifyRequest:
    """Уведомление от сервера так, как его строит `push_payload` (§5.1.2)."""
    payload: dict = {
        "id": 5,
        "title": "Смена закрывается",
        "body": "Сохраните смену и отключите кассу",
        "severity": "important",
        "requires_ack": True,
        "snooze_min": 15,
        "store_id": 12,
    }
    payload.update(overrides)
    return NotifyRequest.model_validate(payload)


SESSION = SessionRef(session_id="1", session_user="user1")


class TestNotificationQueue:
    """Очередь на диске и видимость по сессиям (§7.4, §4.8, П-2)."""

    def test_accept_queues_and_persists(self, tmp_path: Path) -> None:
        queue = build_queue(tmp_path)

        result = queue.accept(notify_request())

        assert result == {"queued": True, "sessions": 0}, "пока никто не входил — сессий нет"
        stored = json.loads(
            (tmp_path / AGENT_NOTIFICATIONS_FILE_NAME).read_text(encoding="utf-8")
        )
        assert stored["notifications"][0]["id"] == 5
        assert stored["notifications"][0]["snooze_min"] == 15, "интервал задаёт сервер (§4.9)"

    def test_acknowledge_survives_reload_and_stops_showing(self, tmp_path: Path) -> None:
        queue = build_queue(tmp_path)
        queue.accept(notify_request())
        assert [item.id for item in queue.pending_for(SESSION)] == [5]

        assert queue.acknowledge(5, SESSION, comment="принял") is True

        # Перезапуск службы: очередь поднимается с диска, «ознакомлен» не теряется
        restarted = build_queue(tmp_path)
        assert restarted.pending_for(SESSION) == []
        state = restarted.state(5, "1")
        assert state is not None
        assert state.is_acknowledged is True
        assert state.comment == "принял"

    def test_snooze_hides_and_returns_after_interval(self, tmp_path: Path) -> None:
        """П-2: отложенное скрывается и всплывает снова, оставаясь непрочитанным."""
        queue = build_queue(tmp_path)
        queue.accept(notify_request())
        moment = utcnow()

        until = queue.snooze(5, SESSION, now=moment)

        assert until == to_iso_z(moment + timedelta(minutes=15))
        assert queue.pending_for(SESSION, now=moment + timedelta(minutes=5)) == []
        visible = queue.pending_for(SESSION, now=moment + timedelta(minutes=16))
        assert [item.id for item in visible] == [5], "отложенное возвращается само (П-2)"
        state = queue.state(5, "1")
        assert state is not None
        assert str(state.status) == "snoozed"
        assert state.snooze_count == 1
        assert state.is_acknowledged is False, "отложенное ознакомлением не считается"

    def test_late_shown_does_not_downgrade_acknowledged(self, tmp_path: Path) -> None:
        queue = build_queue(tmp_path)
        queue.accept(notify_request())
        queue.acknowledge(5, SESSION)

        queue.mark_shown(5, SESSION)

        state = queue.state(5, "1")
        assert state is not None and state.is_acknowledged is True

    def test_deadline_expires_and_stops_showing(self, tmp_path: Path) -> None:
        queue = build_queue(tmp_path)
        queue.accept(notify_request(deadline_at=to_iso_z(utcnow() - timedelta(minutes=1))))

        assert queue.pending_for(SESSION) == []
        state = queue.state(5, "1")
        assert state is not None
        assert str(state.status) == "expired", "дедлайн прошёл — строка закрывается (§4.8)"

    def test_redelivery_keeps_acknowledgement(self, tmp_path: Path) -> None:
        """Повторная доставка (retry диспетчера) не «сбрасывает» ознакомление."""
        queue = build_queue(tmp_path)
        queue.accept(notify_request())
        queue.acknowledge(5, SESSION)

        queue.accept(notify_request(title="Тот же текст, другое время"))

        state = queue.state(5, "1")
        assert state is not None and state.is_acknowledged is True
        assert queue.pending_for(SESSION) == []


def build_agent(  # noqa: ANN201
    tmp_path: Path, *, transport: httpx.AsyncBaseTransport | None = None, **overrides: object
):
    """Приложение агента с временной рабочей папкой, подменёнными процессами и сервером."""
    config = make_config(work_dir=str(tmp_path), **overrides)
    provider = StubProvider()
    app = create_agent_app(
        config,
        config_path=config.work_path / "agent-config.json",
        provider=provider,
        devices_monitor=False,
        notifications_monitor=False,
        notifications_transport=transport,
    )
    return app, config, provider


class TestAgentNotificationsApi:
    """§5.1.2/§7.4: `POST /notify` от сервера и локальные точки TrayAgent."""

    def test_notify_requires_agent_token(self, tmp_path: Path) -> None:
        app, _, _ = build_agent(tmp_path)

        with TestClient(app) as client:
            response = client.post("/notify", json=notify_request().model_dump(mode="json"))

        assert response.status_code == 401

    def test_notify_queues_and_reports_sessions(self, tmp_path: Path) -> None:
        app, _, _ = build_agent(tmp_path)
        payload = notify_request().model_dump(mode="json")

        with TestClient(app) as client:
            first = client.post("/notify", json=payload, headers=HEADERS)

            assert first.status_code == 200
            assert first.json() == {"queued": True, "sessions": 0}, "сессий ещё никто не видел"

            pending = client.get(
                "/notifications/pending", params={"session": "1", "user": "user1"}
            )
            assert pending.status_code == 200

            second = client.post("/notify", json=payload, headers=HEADERS)
            assert second.json()["sessions"] == 1, "сессия стала известна службе (§7.4)"

            health = client.get("/health", headers=HEADERS).json()
            assert health["sessions"] == [{"user": "user1", "sid": "1"}], (
                "«X из Y» считается по сессиям Windows (§4.8, Q50)"
            )

    def test_local_flow_shown_snooze_ack(self, tmp_path: Path) -> None:
        app, _, _ = build_agent(tmp_path)
        ref = {"session_id": "1", "session_user": "user1"}

        with TestClient(app) as client:
            client.post("/notify", json=notify_request().model_dump(mode="json"), headers=HEADERS)

            pending = client.get(
                "/notifications/pending", params={"session": "1", "user": "user1"}
            ).json()
            assert [item["id"] for item in pending["notifications"]] == [5]
            assert pending["notifications"][0]["severity"] == "important"
            assert pending["notifications"][0]["requires_ack"] is True

            shown = client.post("/notifications/5/shown", json=ref)
            assert shown.status_code == 200 and shown.json()["status"] == "shown"

            snoozed = client.post("/notifications/5/snooze", json=ref)
            assert snoozed.json()["status"] == "snoozed"
            assert snoozed.json()["snooze_until"] is not None, "окно скрыто до возврата (П-2)"
            hidden = client.get(
                "/notifications/pending", params={"session": "1", "user": "user1"}
            ).json()
            assert hidden["notifications"] == [], "отложенное не висит на экране (П-2)"

            acked = client.post(
                "/notifications/5/ack", json={**ref, "comment": "прочитал"}
            )
            assert acked.status_code == 200 and acked.json()["status"] == "acknowledged"

    def test_unknown_notification_is_404(self, tmp_path: Path) -> None:
        app, _, _ = build_agent(tmp_path)
        ref = {"session_id": "1", "session_user": "user1"}

        with TestClient(app) as client:
            assert client.post("/notifications/77/ack", json=ref).status_code == 404
            assert client.post("/notifications/77/shown", json=ref).status_code == 404
            assert client.post("/notifications/77/snooze", json=ref).status_code == 404

    def test_local_endpoints_are_loopback_only(self, tmp_path: Path) -> None:
        """§7.4: TrayAgent говорит со службой по `127.0.0.1`, снаружи точки закрыты."""
        app, _, _ = build_agent(tmp_path)
        remote = TestClient(app, client=("10.10.5.77", 50123))
        ref = {"session_id": "1", "session_user": "user1"}

        with remote:
            assert (
                remote.get(
                    "/notifications/pending", params={"session": "1", "user": "user1"}
                ).status_code
                == 403
            )
            assert remote.post("/notifications/5/ack", json=ref).status_code == 403


def build_monitor(  # noqa: ANN201
    tmp_path: Path,
    *,
    status_code: int = 200,
    transport: httpx.AsyncBaseTransport | None = None,
    **overrides: object,
):
    """Очередь + расписание уведомлений с подменённым сервером (§7.4)."""
    server = ScriptedServer(status_code=status_code)
    _, config, provider = build_agent(
        tmp_path, transport=transport if transport is not None else server.transport, **overrides
    )
    monitor = notifications.NotificationMonitor(
        build_queue(tmp_path),
        config=config,
        sender=notifications.ReceiptSender(
            config, transport=transport if transport is not None else server.transport
        ),
        provider=provider,
        startup_delay_sec=0.0,
    )
    return monitor, server, provider


class TestReceiptsAndMsgChannel:
    """Досылка подтверждений и дублирующий канал `msg.exe` (§7.4, П-2)."""

    def test_flush_sends_batch_with_sessions(self, tmp_path: Path) -> None:
        monitor, server, _ = build_monitor(tmp_path)
        monitor.queue.accept(notify_request())
        monitor.queue.acknowledge(5, SESSION)

        sent = asyncio.run(monitor.flush())

        assert sent == 1
        assert server.paths == [notifications.RECEIPTS_STATUS_PATH]
        batch = server.batches[0]
        assert batch["store_id"] == 12
        assert batch["receipts"][0]["status"] == "acknowledged"
        assert batch["sessions"] == [{"user": "user1", "sid": "1"}], (
            "состав сессий едет вместе с подтверждениями (§4.8)"
        )
        assert monitor.queue.dirty_states() == [], "отправленное больше не досылается"

    def test_offline_server_keeps_receipt(self, tmp_path: Path) -> None:
        monitor, server, _ = build_monitor(tmp_path, status_code=503)
        monitor.queue.accept(notify_request())
        monitor.queue.acknowledge(5, SESSION)

        assert asyncio.run(monitor.flush()) == 0

        assert monitor.failed_sends == 1
        assert [item.reported for item in monitor.queue.dirty_states()] == [False], (
            "«Ознакомлен» не потерян, пока сервер молчит (§7.4)"
        )

        server.status_code = 200
        assert asyncio.run(monitor.flush()) == 1
        assert monitor.queue.dirty_states() == []

    def test_poll_interval_comes_from_config(self, tmp_path: Path) -> None:
        monitor, _, _ = build_monitor(tmp_path, notifications_poll_sec=7)

        assert monitor.interval_sec == 7.0, "период сверки — настройка кассы (§7.2)"

#: Настоящий PNG 1×1: касса получает файл целиком, а не «похожие байты» (§7.4, Э9)
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class TestNotificationImagesAndArchive:
    """Э9: картинки в локальном кэше и «справочник» прочитанного (§4.6, §7.4)."""

    def test_pending_carries_markup_and_images(self, tmp_path: Path) -> None:
        """Очередь хранит разметку, имена картинок и признак «удалять после прочтения»."""
        queue = build_queue(tmp_path)
        queue.accept(
            notify_request(
                body_format="html",
                body='<p>Схема зала</p><img src="magaz-image:shema.png">',
                images=["shema.png", "../secrets.key"],
                delete_after_read=True,
            )
        )

        item = queue.pending_for(SESSION)[0]

        assert item.body_format == "html"
        assert item.images == ["shema.png"], "небезопасное имя в кэш не попадает"
        assert item.delete_after_read is True

    def test_archive_keeps_only_read_and_kept(self, tmp_path: Path) -> None:
        """В «справочнике» — прочитанное и не помеченное «удалять после прочтения»."""
        queue = build_queue(tmp_path)
        queue.accept(notify_request(id=5, title="Как закрыть смену"))
        queue.accept(notify_request(id=6, title="Разовая рассылка", delete_after_read=True))
        queue.acknowledge(5, SESSION)
        queue.acknowledge(6, SESSION)

        archive = queue.archive_for(SESSION)

        assert [item.id for item in archive] == [5]
        assert archive[0].title == "Как закрыть смену"
        assert archive[0].delete_after_read is False

    def test_archive_is_empty_until_acknowledged(self, tmp_path: Path) -> None:
        queue = build_queue(tmp_path)
        queue.accept(notify_request())

        assert queue.archive_for(SESSION) == []

    def test_image_cache_stores_and_reads(self, tmp_path: Path) -> None:
        cache = notifications.NotificationImageCache(tmp_path / "images")

        assert cache.has(5, "shema.png") is False
        path = cache.store(5, "shema.png", PNG_1PX)

        assert path is not None
        assert path.parent.name == "5"
        assert cache.has(5, "shema.png") is True
        assert cache.read(5, "shema.png") == PNG_1PX
        assert cache.read(5, "net.png") is None

    def test_image_cache_rejects_unsafe_name(self, tmp_path: Path) -> None:
        """Имя из тела проверяется и на кассе: файлы не читаются вне каталога кэша."""
        cache = notifications.NotificationImageCache(tmp_path / "images")

        assert cache.path(5, "../secrets.key") is None
        assert cache.store(5, "../secrets.key", PNG_1PX) is None
        assert cache.read(5, "../secrets.key") is None

    def test_media_type_by_extension(self) -> None:
        assert notifications.image_media_type("a.png") == "image/png"
        assert notifications.image_media_type("foto.JPEG") == "image/jpeg"
        assert notifications.image_media_type("a.bin") == "application/octet-stream"






    def test_downloader_fetches_missing_and_skips_cached(self, tmp_path: Path) -> None:
        """Скачиваем только то, чего нет в кэше: без сети картинка уже на месте."""
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(request.url.path)
            return httpx.Response(200, content=PNG_1PX)

        cache = notifications.NotificationImageCache(tmp_path / "images")
        cache.store(5, "est.png", PNG_1PX)
        downloader = notifications.ImageDownloader(
            make_config(work_dir=str(tmp_path)),
            cache=cache,
            transport=httpx.MockTransport(handler),
        )

        stored = asyncio.run(downloader.fetch(5, ["est.png", "net.png"]))

        assert stored == 1
        assert requested == ["/api/v1/agent/notifications/5/images/net.png"]
        assert cache.read(5, "net.png") == PNG_1PX

    def test_downloader_tolerates_refusal_and_offline(self, tmp_path: Path) -> None:
        """Отказ сервера и обрыв сети — не ошибка доставки: уведомление уже в очереди."""
        cache = notifications.NotificationImageCache(tmp_path / "images")

        def refuse(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "нет картинки"})

        refused = notifications.ImageDownloader(
            make_config(work_dir=str(tmp_path)),
            cache=cache,
            transport=httpx.MockTransport(refuse),
        )
        assert asyncio.run(refused.fetch(5, ["net.png"])) == 0

        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("сеть недоступна", request=request)

        offline = notifications.ImageDownloader(
            make_config(work_dir=str(tmp_path)),
            cache=cache,
            transport=httpx.MockTransport(broken),
        )
        assert asyncio.run(offline.fetch(5, ["net.png"])) == 0
        assert cache.has(5, "net.png") is False


class TestImagesAndArchiveApi:
    """Локальные точки для окна кассы: картинка из кэша и «справочник» (§7.4, Э9)."""

    def test_notify_downloads_images_into_cache(self, tmp_path: Path) -> None:
        """`POST /notify` тянет картинки фоном — окно покажет их без сети."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/images/shema.png"):
                return httpx.Response(200, content=PNG_1PX)
            return httpx.Response(200, json={"ok": True})

        app, _, _ = build_agent(tmp_path, transport=httpx.MockTransport(handler))

        with TestClient(app) as client:
            response = client.post(
                "/notify",
                json=notify_request(images=["shema.png"]).model_dump(mode="json"),
                headers=HEADERS,
            )
            assert response.status_code == 200
            cache = app.state.agent_image_cache
            for _ in range(100):
                if cache.has(5, "shema.png"):
                    break
                time.sleep(0.05)

        assert cache.read(5, "shema.png") == PNG_1PX

    def test_image_endpoint_serves_cache(self, tmp_path: Path) -> None:
        app, _, _ = build_agent(tmp_path)

        with TestClient(app) as client:
            app.state.agent_image_cache.store(5, "shema.png", PNG_1PX)
            response = client.get("/notifications/5/images/shema.png")

        assert response.status_code == 200
        assert response.content == PNG_1PX
        assert response.headers["content-type"] == "image/png"

    def test_image_endpoint_is_404_without_cache(self, tmp_path: Path) -> None:
        app, _, _ = build_agent(tmp_path)

        with TestClient(app) as client:
            assert client.get("/notifications/5/images/net.png").status_code == 404

    def test_archive_endpoint_lists_read_notifications(self, tmp_path: Path) -> None:
        app, _, _ = build_agent(tmp_path)
        ref = {"session_id": "1", "session_user": "user1"}

        with TestClient(app) as client:
            client.post(
                "/notify",
                json=notify_request(title="Как закрыть смену").model_dump(mode="json"),
                headers=HEADERS,
            )
            empty = client.get(
                "/notifications/archive", params={"session": "1", "user": "user1"}
            ).json()
            client.post("/notifications/5/ack", json=ref)
            archive = client.get(
                "/notifications/archive", params={"session": "1", "user": "user1"}
            ).json()["notifications"]

        assert empty["notifications"] == []
        assert [item["id"] for item in archive] == [5]
        assert archive[0]["title"] == "Как закрыть смену"

    def test_new_endpoints_are_loopback_only(self, tmp_path: Path) -> None:
        app, _, _ = build_agent(tmp_path)
        remote = TestClient(app, client=("10.10.5.77", 50123))

        with remote:
            assert (
                remote.get(
                    "/notifications/archive", params={"session": "1", "user": "user1"}
                ).status_code
                == 403
            )
            assert remote.get("/notifications/5/images/a.png").status_code == 403
