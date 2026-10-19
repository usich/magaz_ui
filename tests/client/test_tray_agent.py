"""TrayAgent: показ уведомлений в сессии пользователя (§7.4, Э6; читалка — 1.21).

Проверки идут без GUI и без сети: Qt (`PySide6`) не участвует (окна
подменяются двойником), служба агента — `httpx.MockTransport`. Проверяется то,
что важно на кассе:

* трей говорит только с `127.0.0.1` и без токена (§7.4, §8.1);
* уведомление любого уровня всплывает **тостом с кнопками** («Просмотреть» /
  «Отложить»), сам тост не исчезает и окон не открывает (1.21);
* `critical` с `repeat_every_min` всплывает тостом снова по интервалу (П-2);
* «Ознакомлен» и «Отложить» уходят в службу и только после успеха убирают
  уведомление из трея; «Ознакомлен» открывает меню категорий, «Отложить» — нет;
* недоступность службы — не ошибка: трей просто ждёт следующего опроса.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

from client.tray import __main__ as tray_main
from client.tray.api import DEFAULT_AGENT_URL, AgentUnreachable, TrayAgentClient
from client.tray.presenter import (
    GROUP_BY_CATEGORY,
    GROUP_BY_SEVERITY,
    KIND_TOAST,
    NO_CATEGORY_TITLE,
    SEVERITY_COLORS,
    TAB_ALL,
    TAB_ARCHIVE,
    TrayPresenter,
    build_tabs,
    group_items,
    items_for_tab,
    panel_geometry,
)
from client.tray.session import SESSION_ENV, UNKNOWN_SESSION_ID, USER_ENV, current_session_id, current_session_user, session_ref
from shared.enums import NotificationSeverity
from shared.schemas import NotificationCategoryRef, NotificationPendingItem, SessionRef
from shared.timeutil import utcnow

SESSION = SessionRef(session_id="7", session_user="kassir")


def make_pending(
    notification_id: int = 1,
    *,
    severity: NotificationSeverity = NotificationSeverity.INFO,
    repeat_every_min: int | None = None,
    title: str = "Смена закрывается в 22:00",
    snooze_until: str | None = None,
    category: NotificationCategoryRef | None = None,
) -> NotificationPendingItem:
    """Уведомление в том виде, в каком его отдаёт служба (§5.1.2)."""
    return NotificationPendingItem(
        id=notification_id,
        title=title,
        body="Сохраните смену и отключите кассу",
        severity=severity,
        category=category,
        requires_ack=severity is not NotificationSeverity.INFO,
        repeat_every_min=repeat_every_min,
        snooze_until=snooze_until,
    )


def make_category(
    category_id: int = 1, *, name: str = "Продажи", color: str = "#2563eb"
) -> NotificationCategoryRef:
    """Категория уведомления так, как её отдаёт служба на кассу (1.22)."""
    return NotificationCategoryRef(id=category_id, name=name, color=color)


class FakeClient:
    """Двойник клиента службы: записывает вызовы и умеет «падать».

    Нужен, чтобы проверить поведение трея без HTTP: подмена одной точки
    (`TrayAgentClient`) не требует ни сети, ни прав на кассе (I-11).
    """

    def __init__(
        self,
        items: list[NotificationPendingItem] | None = None,
        *,
        receipts_ok: bool = True,
        pending_fails: bool = False,
    ) -> None:
        self.items = items or []
        self.receipts_ok = receipts_ok
        self.pending_fails = pending_fails
        self.pending_calls: list[SessionRef] = []
        self.shown_calls: list[int] = []
        self.ack_calls: list[int] = []
        self.snooze_calls: list[tuple[int, int | None]] = []
        self.archive_items: list[NotificationPendingItem] = []
        self.archive_calls = 0
        self.image_bytes: bytes | None = None
        self.closed = False

    def pending(self, ref: SessionRef) -> list[NotificationPendingItem]:
        self.pending_calls.append(ref)
        if self.pending_fails:
            raise AgentUnreachable("служба не отвечает")
        return list(self.items)

    def shown(self, notification_id: int, ref: SessionRef) -> bool:
        _ = ref
        if self.receipts_ok:
            self.shown_calls.append(notification_id)
        return self.receipts_ok

    def ack(self, notification_id: int, ref: SessionRef, *, comment: str | None = None) -> bool:
        _ = ref, comment
        if self.receipts_ok:
            self.ack_calls.append(notification_id)
        return self.receipts_ok

    def snooze(self, notification_id: int, ref: SessionRef, *, snooze_min: int | None = None) -> bool:
        _ = ref
        if self.receipts_ok:
            self.snooze_calls.append((notification_id, snooze_min))
        return self.receipts_ok

    def archive(self, ref: SessionRef) -> list[NotificationPendingItem]:
        """`GET /notifications/archive` — справочник прочитанного (§7.4, Э9)."""
        _ = ref
        self.archive_calls += 1
        return list(self.archive_items)

    def image(self, notification_id: int, name: str) -> bytes | None:
        """Картинка из кэша кассы; `None` — служба её не отдала (§7.4, Э9)."""
        _ = notification_id, name
        return self.image_bytes

    def close(self) -> None:
        self.closed = True


class FakeWindows:
    """Двойник окон трея: запоминает, что «показано», и не создаёт GUI."""

    def __init__(self) -> None:
        self.toasts: list[int] = []
        self.windows: list[int] = []
        self.topmost: list[bool] = []
        self.readers: list[int] = []
        self.beeps = 0
        self.unread: list[int] = []
        self.archive: list[int] = []
        self.hidden = True
        self.quit_called = False
        self.on_show = None

    def toast(self, item: NotificationPendingItem, *, seconds: int = 3) -> None:
        _ = seconds
        self.toasts.append(item.id)

    def window(self, item: NotificationPendingItem, *, topmost: bool = False, sound: bool = False) -> None:
        _ = sound
        self.windows.append(item.id)
        self.topmost.append(topmost)

    def reader(self, item: NotificationPendingItem) -> None:
        """Кнопка «Просмотреть» в тосте: окно-читалка с полным телом (§7.4, 1.21)."""
        self.readers.append(item.id)

    def beep(self) -> None:
        """Звук тоста `critical` (в том числе при повторе)."""
        self.beeps += 1

    def set_unread(self, items: list[NotificationPendingItem]) -> None:
        self.unread.append(len(items))

    def set_archive(self, items: list[NotificationPendingItem]) -> None:
        self.archive = [item.id for item in items]

    @property
    def is_open(self) -> bool:
        """Окно показано: только тогда трей читает «справочник» (§7.4, Э9)."""
        return not self.hidden

    def hide(self) -> None:
        self.hidden = True

    def show_unread(self, items: list[NotificationPendingItem]) -> None:
        self.hidden = False
        for item in items:
            self.window(item)

    # Кнопки окон: двойник вызывает те же обработчики, что и `TrayWindows`
    def click_view(self, item: NotificationPendingItem) -> None:
        """«Просмотреть» в тосте — открыть читалку."""
        if self.on_show is not None:
            self.on_show(item)

    def after(self, delay_ms: int, callback) -> None:  # noqa: ANN001 - callback
        _ = delay_ms, callback

    def pump(self) -> None:
        """Цикл окон в тестах не запускается: проверяется опрос, а не GUI."""

    def quit(self) -> None:
        self.quit_called = True

    def destroy(self) -> None:
        """Ресурсов у двойника нет."""


class TestSession:
    """Сессия Windows, от имени которой говорит трей (§7.4, Q50, П-6)."""

    def test_env_overrides_give_exact_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SESSION_ENV, "7")
        monkeypatch.setenv(USER_ENV, "kassir")

        ref = session_ref()

        assert (ref.session_id, ref.session_user) == ("7", "kassir")

    def test_session_id_is_always_filled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SESSION_ENV, raising=False)
        monkeypatch.setenv(USER_ENV, "kassir")

        session_id = current_session_id()

        # На Windows это номер сессии из `ProcessIdToSessionId`, в остальных
        # случаях — запасное значение: пустой `SessionRef` служба отвергнет
        assert session_id.strip()
        assert session_ref().session_id == session_id

    def test_user_falls_back_to_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(USER_ENV, " operator ")
        assert current_session_user() == "operator"


def make_loopback_client(handler) -> TrayAgentClient:  # noqa: ANN001 - handler httpx
    """Клиент службы на подменённом транспорте: реальный сокет не открывается."""
    return TrayAgentClient(DEFAULT_AGENT_URL, transport=httpx.MockTransport(handler))


class TestLoopbackClient:
    """Трей говорит только с `127.0.0.1` и без токена агента (§7.4, §8.1)."""

    def test_pending_asks_local_service_for_session(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["host"] = request.url.host or ""
            seen["url"] = str(request.url)
            seen["token"] = request.headers.get("x-agent-token", "")
            return httpx.Response(
                200, json={"notifications": [make_pending().model_dump(mode="json")]}
            )

        client = make_loopback_client(handler)
        items = client.pending(SESSION)

        assert seen["host"] == "127.0.0.1", "обращаемся только к локальной службе"
        assert seen["token"] == "", "токена агента у трея нет"
        assert "session=7" in seen["url"]
        assert "user=kassir" in seen["url"]
        assert [item.id for item in items] == [1]
        assert items[0].severity == NotificationSeverity.INFO

    def test_pending_survives_service_downtime(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001 - транспорт
            raise httpx.ConnectError("служба не слушает порт")

        client = make_loopback_client(handler)

        with pytest.raises(AgentUnreachable):
            client.pending(SESSION)

    def test_pending_reports_http_error_as_unreachable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001 - транспорт
            return httpx.Response(503, json={"detail": "идёт операция 1С"})

        client = make_loopback_client(handler)

        with pytest.raises(AgentUnreachable):
            client.pending(SESSION)

    def test_ack_sends_session_and_comment(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"ok": True, "status": "acknowledged"})

        client = make_loopback_client(handler)

        assert client.ack(5, SESSION, comment="ознакомлен на кассе") is True
        assert captured["method"] == "POST"
        assert captured["path"] == "/notifications/5/ack"
        assert captured["body"] == {
            "session_id": "7",
            "session_user": "kassir",
            "comment": "ознакомлен на кассе",
        }

    def test_ack_without_comment_keeps_body_minimal(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"ok": True})

        client = make_loopback_client(handler)
        client.ack(5, SESSION)

        assert captured["body"] == {"session_id": "7", "session_user": "kassir"}

    def test_snooze_sends_interval_only_when_given(self) -> None:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True, "status": "snoozed"})

        client = make_loopback_client(handler)

        assert client.snooze(5, SESSION) is True
        assert client.snooze(5, SESSION, snooze_min=15) is True
        # Интервал возврата — настройка сервера (П-2): трей его не выдумывает
        assert bodies[0] == {"session_id": "7", "session_user": "kassir"}
        assert bodies[1]["snooze_min"] == 15

    def test_shown_marks_toast(self) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(200, json={"ok": True, "status": "shown"})

        client = make_loopback_client(handler)

        assert client.shown(9, SESSION) is True
        assert paths == ["/notifications/9/shown"]

    def test_receipts_are_false_when_service_is_gone(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001 - транспорт
            raise httpx.ConnectError("служба недоступна")

        client = make_loopback_client(handler)

        # Уведомление останется видимым: окно закрывается только после успеха
        assert client.shown(1, SESSION) is False
        assert client.ack(1, SESSION) is False
        assert client.snooze(1, SESSION) is False

class TestPresenter:
    """Что и когда показывать (§7.4, 1.21): тост для всех уровней и повтор `critical`."""

    def test_any_level_is_toasted_once(self) -> None:
        """С 1.21 и «информация», и «важное» всплывают тостом — окна открывает кнопка."""
        for severity in (
            NotificationSeverity.INFO,
            NotificationSeverity.IMPORTANT,
            NotificationSeverity.CRITICAL,
        ):
            presenter = TrayPresenter()
            item = make_pending(severity=severity)

            first = presenter.decide([item])
            second = presenter.decide([item])

            assert [action.kind for action in first] == [KIND_TOAST], severity
            assert first[0].is_toast is True
            assert second == [], "тост второй раз не всплывает"
            assert presenter.unread_count == 1, "уведомление ждёт кнопку в трее"

    def test_critical_repeats_after_interval(self) -> None:
        presenter = TrayPresenter()
        item = make_pending(severity=NotificationSeverity.CRITICAL, repeat_every_min=5)
        start = utcnow()

        assert [action.kind for action in presenter.decide([item], now=start)] == [KIND_TOAST]
        assert presenter.decide([item], now=start + timedelta(minutes=4)) == []
        repeat = presenter.decide([item], now=start + timedelta(minutes=5, seconds=1))

        assert [action.kind for action in repeat] == [KIND_TOAST], "повтор тоже тостом"
        assert repeat[0].notification.id == item.id

    def test_critical_without_interval_does_not_repeat(self) -> None:
        presenter = TrayPresenter()
        item = make_pending(severity=NotificationSeverity.CRITICAL, repeat_every_min=None)
        start = utcnow()

        presenter.decide([item], now=start)
        later = presenter.decide([item], now=start + timedelta(hours=1))

        assert later == [], "без интервала уведомление ждёт кнопку «Ознакомлен»"

    def test_important_does_not_repeat(self) -> None:
        """Повтор — только у `critical`: «важное» не должно всплывать само снова."""
        presenter = TrayPresenter()
        item = make_pending(severity=NotificationSeverity.IMPORTANT, repeat_every_min=5)
        start = utcnow()

        presenter.decide([item], now=start)
        later = presenter.decide([item], now=start + timedelta(minutes=30))

        assert later == []

    def test_acknowledged_notification_is_forgotten(self) -> None:
        presenter = TrayPresenter()
        item = make_pending(severity=NotificationSeverity.INFO)
        presenter.decide([item])

        # Служба перестала отдавать уведомление: его подтвердили или отложили
        assert presenter.decide([]) == []
        assert presenter.unread_count == 0
        # Если уведомление с тем же id придёт снова, тост повторится
        assert [action.kind for action in presenter.decide([item])] == [KIND_TOAST]

    def test_forget_removes_notification_from_menu(self) -> None:
        presenter = TrayPresenter()
        item = make_pending(severity=NotificationSeverity.IMPORTANT)
        presenter.decide([item])

        presenter.forget(item.id)

        assert presenter.visible == []
        assert presenter.unread_count == 0

    def test_visible_keeps_order_of_service_response(self) -> None:
        presenter = TrayPresenter()
        items = [make_pending(2, title="Второе"), make_pending(1, title="Первое")]

        presenter.decide(items)

        assert [item.id for item in presenter.visible] == [2, 1]


class TestTrayAgent:
    """Цикл трея: опрос службы, всплытие тоста и подтверждения (§7.4, 1.21)."""

    def build(self, **kwargs) -> tuple[tray_main.TrayAgent, FakeClient, FakeWindows]:  # noqa: ANN003
        """Трей с двойниками службы и окон — GUI и сеть не участвуют.

        Кнопке «Просмотреть» выдаётся тот же обработчик, что у настоящего
        `TrayWindows`: тост открывает читалку, а не окно по своей воле.
        """
        client = kwargs.pop("client", None) or FakeClient(**kwargs)
        windows = FakeWindows()
        agent = tray_main.TrayAgent(client, windows=windows)
        windows.on_show = windows.reader  # «Просмотреть» открывает читалку
        return agent, client, windows

    def test_toast_is_shown_and_marked_shown(self) -> None:
        agent, client, windows = self.build(
            items=[make_pending(severity=NotificationSeverity.INFO)]
        )

        actions = agent.poll_once()

        assert [action.kind for action in actions] == [KIND_TOAST]
        assert windows.toasts == [1]
        assert client.shown_calls == [1], "тост считается показанным (§7.4)"
        assert client.pending_calls[0].session_user, "сессия передана службе"

    def test_important_also_toasts_and_opens_no_window(self) -> None:
        """С 1.21 «важное» не открывает окно само: сначала тост с кнопками."""
        agent, client, windows = self.build(
            items=[make_pending(severity=NotificationSeverity.IMPORTANT)]
        )

        agent.poll_once()

        assert windows.toasts == [1]
        assert windows.windows == [], "окна трей сам не открывает"
        assert windows.readers == [], "читалка — только по кнопке «Просмотреть»"
        assert client.shown_calls == [1], "и «важное» фиксируется как показанное"

    def test_critical_toasts_and_beeps(self) -> None:
        agent, _client, windows = self.build(
            items=[make_pending(severity=NotificationSeverity.CRITICAL, repeat_every_min=5)]
        )

        agent.poll_once()

        assert windows.toasts == [1], "критичное тоже всплывает тостом"
        assert windows.beeps == 1, "критичное сопровождается звуком"
        assert windows.windows == []

    def test_view_button_opens_reader(self) -> None:
        """«Просмотреть» в тосте открывает читалку с полным телом (§7.4, 1.21)."""
        agent, _client, windows = self.build(
            items=[make_pending(severity=NotificationSeverity.IMPORTANT)]
        )
        agent.poll_once()

        windows.click_view(agent.presenter.visible[0])

        assert windows.readers == [1]

    def test_ack_moves_notification_into_menu(self) -> None:
        """«Ознакомлен» убирает уведомление из непрочитанных и открывает меню."""
        agent, client, windows = self.build(
            items=[make_pending(1, severity=NotificationSeverity.IMPORTANT)]
        )
        client.items = [make_pending(2, title="Второе")]
        agent.poll_once()

        agent.acknowledge(1)
        agent.poll_once()

        assert client.ack_calls == [1]
        assert [item.id for item in agent.presenter.visible] == [2]
        assert windows.unread[-1] == 1, "в меню категорий виден остаток, включая непрочитанное"

    def test_service_downtime_is_not_an_error(self) -> None:
        agent, client, windows = self.build(pending_fails=True)

        assert agent.poll_once() == []
        assert windows.toasts == []
        assert client.pending_calls, "трей честно попробовал опросить службу"

    def test_acknowledge_sends_receipt_and_forgets(self) -> None:
        agent, client, windows = self.build(
            items=[make_pending(severity=NotificationSeverity.IMPORTANT)]
        )
        agent.poll_once()

        assert agent.acknowledge(1) is True

        assert client.ack_calls == [1]
        assert agent.presenter.visible == []
        assert windows.unread[-1] == 0

    def test_snooze_sends_receipt_and_forgets(self) -> None:
        agent, client, _windows = self.build(
            items=[make_pending(severity=NotificationSeverity.IMPORTANT)]
        )
        agent.poll_once()

        assert agent.snooze(1, snooze_min=15) is True

        assert client.snooze_calls == [(1, 15)]
        assert agent.presenter.visible == []

    def test_failed_receipt_keeps_notification_visible(self) -> None:
        agent, _client, _windows = self.build(
            items=[make_pending(severity=NotificationSeverity.IMPORTANT)], receipts_ok=False
        )
        agent.poll_once()

        assert agent.acknowledge(1) is False
        assert agent.presenter.unread_count == 1, "без подтверждения уведомление не теряется"

    def test_console_mode_prints_instead_of_windows(self) -> None:
        client = FakeClient(items=[make_pending(severity=NotificationSeverity.IMPORTANT)])
        agent = tray_main.TrayAgent(client, console=True)

        actions = agent.poll_once()

        assert [action.kind for action in actions] == [KIND_TOAST]
        assert client.shown_calls == []


class TestTrayCli:
    """Ключи командной строки трея (§7.5): режимы отладки и подтверждения."""

    def patch_client(
        self, monkeypatch: pytest.MonkeyPatch, client: FakeClient
    ) -> FakeClient:
        """Подменить клиент службы: `main()` не должен лезть в сеть."""
        monkeypatch.setattr(tray_main, "TrayAgentClient", lambda *args, **kwargs: client)
        return client

    def test_pending_prints_list(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.patch_client(
            monkeypatch,
            FakeClient(
                items=[
                    make_pending(1, title="Первое", severity=NotificationSeverity.IMPORTANT),
                    make_pending(2, title="Второе", snooze_until="2026-10-14T12:00:00Z"),
                ]
            ),
        )

        assert tray_main.main(["--pending"]) == 0

        output = capsys.readouterr().out
        assert "#1 [important] Первое" in output
        assert "#2 [info] Второе" in output
        assert "вернётся после 2026-10-14T12:00:00Z" in output

    def test_pending_reports_unreachable_service(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.patch_client(monkeypatch, FakeClient(pending_fails=True))

        assert tray_main.main(["--pending"]) == 2

        assert "Служба агента недоступна" in capsys.readouterr().out

    def test_ack_sends_receipt_and_reports_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self.patch_client(monkeypatch, FakeClient())

        assert tray_main.main(["--ack", "5"]) == 0
        assert client.ack_calls == [5]

    def test_snooze_reports_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self.patch_client(monkeypatch, FakeClient(receipts_ok=False))

        assert tray_main.main(["--snooze", "5"]) == 2
        assert client.snooze_calls == []

    def test_once_with_nothing_to_show(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.patch_client(monkeypatch, FakeClient())

        assert tray_main.main(["--once"]) == 0

        assert "Показывать нечего" in capsys.readouterr().out

    def test_parser_defaults_point_to_loopback(self) -> None:
        args = tray_main.build_parser().parse_args([])

        assert args.agent_url == DEFAULT_AGENT_URL
        assert args.agent_url.startswith("http://127.0.0.1")
        assert args.interval == tray_main.DEFAULT_INTERVAL_SEC
        assert args.console is False


#: Прокси, которого нет: если httpx послушает окружение, запрос к кассе уйдёт сюда
BROKEN_PROXY = "http://127.0.0.1:9"


class _StubAgent(BaseHTTPRequestHandler):
    """Мини-служба агента: отвечает «уведомлений нет» (как `stub_agent` у сервера)."""

    def do_GET(self) -> None:  # noqa: N802 - имя задано базовым классом
        payload = json.dumps({"notifications": []}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        """Молча: вывод тестов не засоряем."""


@pytest.fixture()
def stub_agent_url() -> Iterator[str]:
    """Адрес живой мини-службы на `127.0.0.1` со свободным портом."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubAgent)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


class TestProxyIsIgnored:
    """Трей ходит на `127.0.0.1` в обход системного прокси.

    Живой прогон Э7: на машине с системным прокси httpx отвечал `503` от прокси, и
    уведомления до кассы не доходили (служба при этом была жива). Это та же ловушка,
    что закрыта в `AgentClient`, — `trust_env=False` обязателен и здесь. Проверка
    идёт против настоящей службы на сокете: с `httpx.MockTransport` прокси не
    участвует и дефект не воспроизводится.
    """

    def test_client_is_created_without_env_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}
        real_client = httpx.Client

        def fake_client(*args: object, **kwargs: object) -> httpx.Client:
            captured.update(kwargs)
            return real_client(*args, **kwargs)

        monkeypatch.setattr("client.tray.api.httpx.Client", fake_client)

        TrayAgentClient()

        assert captured["trust_env"] is False
        assert str(captured["base_url"]).startswith("http://127.0.0.1")

    def test_request_goes_directly_to_agent(
        self, stub_agent_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HTTP_PROXY", BROKEN_PROXY)
        monkeypatch.setenv("HTTPS_PROXY", BROKEN_PROXY)
        monkeypatch.setenv("ALL_PROXY", BROKEN_PROXY)

        client = TrayAgentClient(base_url=stub_agent_url, timeout_sec=3.0)

        assert client.pending(SessionRef(session_id="1", session_user="dev")) == []


class TestTrayPanel:
    """Окно уведомлений: вкладки, «справочник» и геометрия «не больше 15 %» (§7.4, Э9)."""

    def test_tabs_count_unread_and_archive(self) -> None:
        unread = [
            make_pending(1, severity=NotificationSeverity.IMPORTANT),
            make_pending(2, severity=NotificationSeverity.IMPORTANT),
            make_pending(3, severity=NotificationSeverity.CRITICAL),
        ]
        archive = [make_pending(4)]

        tabs = build_tabs(unread, archive)

        assert [(tab.key, tab.count) for tab in tabs] == [
            (TAB_ALL, 3),
            ("info", 0),
            ("important", 2),
            ("critical", 1),
            (TAB_ARCHIVE, 1),
        ]
        assert tabs[-1].is_archive is True
        assert tabs[0].is_archive is False

    def test_items_for_tab_filters_by_severity_and_archive(self) -> None:
        unread = [
            make_pending(1, severity=NotificationSeverity.INFO),
            make_pending(2, severity=NotificationSeverity.CRITICAL),
        ]
        archive = [make_pending(9)]

        assert [item.id for item in items_for_tab(TAB_ALL, unread, archive)] == [1, 2]
        assert [item.id for item in items_for_tab("critical", unread, archive)] == [2]
        assert [item.id for item in items_for_tab("important", unread, archive)] == []
        assert [item.id for item in items_for_tab(TAB_ARCHIVE, unread, archive)] == [9]

    def test_panel_is_top_right_and_within_15_percent(self) -> None:
        x, y, width, height = panel_geometry(1920, 1080)

        assert y >= 0
        assert x + width <= 1920, "окно прижато к правому краю (§7.4)"
        assert x >= 1920 - width - 12
        assert width * height <= 0.15 * 1920 * 1080, "не больше 15 % площади экрана"

    def test_panel_fits_small_screen(self) -> None:
        x, y, width, height = panel_geometry(1024, 768)

        assert 0 <= x and x + width <= 1024
        assert 0 <= y and y + height <= 768
        assert width * height <= 0.15 * 1024 * 768

    def test_presenter_tabs_use_archive(self) -> None:
        presenter = TrayPresenter()
        presenter.decide([make_pending(1, severity=NotificationSeverity.INFO)])
        presenter.set_archive([make_pending(2)])

        counts = {tab.key: tab.count for tab in presenter.tabs()}

        assert counts[TAB_ALL] == 1
        assert counts["info"] == 1
        assert counts[TAB_ARCHIVE] == 1
        assert [item.id for item in presenter.archive] == [2]

    def test_group_by_severity_orders_and_colors(self) -> None:
        """Группировка по важности: критично → важно → информация, цвет по уровню (§7.4, 1.22)."""
        items = [
            make_pending(1, severity=NotificationSeverity.INFO),
            make_pending(2, severity=NotificationSeverity.CRITICAL),
            make_pending(3, severity=NotificationSeverity.IMPORTANT),
        ]

        groups = group_items(items, by=GROUP_BY_SEVERITY)

        assert [group.key for group in groups] == ["critical", "important", "info"]
        assert [group.count for group in groups] == [1, 1, 1]
        assert groups[0].color == SEVERITY_COLORS["critical"], "критично — красный"

    def test_group_by_category_keeps_order_and_unknown_bucket(self) -> None:
        """Группировка по категории: уведомление без категории — в отдельной группе (§7.4, 1.22)."""
        sales = make_category(1, name="Продажи", color="#2563eb")
        purchases = make_category(2, name="Закупки", color="#10b981")
        items = [
            make_pending(1, category=sales),
            make_pending(2, category=purchases),
            make_pending(3, category=sales),
            make_pending(4),
        ]

        groups = group_items(items, by=GROUP_BY_CATEGORY)

        assert [(group.title, group.count) for group in groups] == [
            ("Продажи", 2),
            ("Закупки", 1),
            (NO_CATEGORY_TITLE, 1),
        ]
        assert groups[0].color == "#2563eb", "цвет плашки — из категории"
        assert [item.id for item in groups[0].items] == [1, 3]

    def test_group_by_category_empty_list_is_empty(self) -> None:
        """Пустой список не рождает пустых групп — окно не рисует заголовков зря."""
        assert group_items([], by=GROUP_BY_CATEGORY) == []
        assert group_items([], by=GROUP_BY_SEVERITY) == []

    def test_group_by_severity_omits_empty_levels(self) -> None:
        """Уровень без уведомлений не показывается: список не пухнет пустыми группами."""
        groups = group_items([make_pending(1, severity=NotificationSeverity.INFO)])

        assert [group.key for group in groups] == ["info"]

    def test_agent_reads_archive_even_when_panel_is_closed(self) -> None:
        """Архив читается всегда: меню после «Ознакомлен» открывается сразу (§7.4, 1.21-fix).

        Раньше справочник читался только при открытом окне, и уведомление с
        `delete_after_read=false` не успевало попасть в меню — кассир видел
        пустой справочник сразу после «Ознакомлен».
        """
        client = FakeClient([make_pending(1, severity=NotificationSeverity.IMPORTANT)])
        client.archive_items = [make_pending(2)]
        windows = FakeWindows()
        agent = tray_main.TrayAgent(client, presenter=TrayPresenter(), windows=windows)

        agent.poll_once()

        assert client.archive_calls == 1, "справочник читается и при закрытом окне"
        assert windows.unread[-1] == 1, "счётчик непрочитанных тоже обновляется"
        assert windows.archive == [2], "окно знает справочник до открытия меню"

    def test_acknowledged_notification_stays_in_archive(self) -> None:
        """«Ознакомлен» не стирает уведомление из справочника (§7.4, Э9, баг 1.21).

        Сценарий заказчика: уведомление с `delete_after_read=false` прочитано —
        оно обязано остаться на вкладке «справочник», чтобы кассир вернулся к
        инструкции позже.
        """
        kept = make_pending(5, title="Как закрыть смену")
        client = FakeClient([kept])
        windows = FakeWindows()
        agent = tray_main.TrayAgent(client, presenter=TrayPresenter(), windows=windows)
        agent.poll_once()

        # Служба перестала отдавать уведомление в pending, но оставила в archive
        client.items = []
        client.archive_items = [kept]

        assert agent.acknowledge(5) is True

        assert agent.presenter.visible == [], "уведомление ушло из непрочитанных"
        assert [item.id for item in agent.presenter.archive] == [5], "осталось в справочнике"
        assert windows.archive == [5], "окно получило свежий справочник сразу после ack"

