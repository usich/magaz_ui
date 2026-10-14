"""Клиент локальных точек агента (§5.1.2, §7.4): только `127.0.0.1`, без токена.

TrayAgent не ходит в сеть: он знает один адрес — локальную службу. Поэтому
здесь нет ни токена, ни ретраев «до сервера»: если служба недоступна, трей
просто тихо ждёт следующего опроса, а уведомление остаётся в очереди на диске
(`notifications.json`) и появится, когда служба поднимется.

Все ошибки сети собраны в `AgentUnreachable`, чтобы вызывающий код (трей) не
разбирал исключения `httpx` вручную.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from shared.schemas import NotificationPendingItem, SessionRef

__all__ = [
    "DEFAULT_AGENT_URL",
    "DEFAULT_TIMEOUT_SEC",
    "AgentUnreachable",
    "TrayAgentClient",
]

#: Адрес локальной службы агента (§7.1: порт по умолчанию 8383)
DEFAULT_AGENT_URL = "http://127.0.0.1:8383"
DEFAULT_TIMEOUT_SEC = 3.0


class AgentUnreachable(RuntimeError):
    """Локальная служба не ответила (выключена, перезапускается, порт занят)."""


class TrayAgentClient:
    """Синхронный клиент локальных точек: трей работает в своём процессе, async ни к чему."""

    def __init__(
        self,
        base_url: str = DEFAULT_AGENT_URL,
        *,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout_sec,
            transport=transport,
            # Прокси не должен вмешиваться: трей ходит только на 127.0.0.1, а httpx
            # по умолчанию берёт прокси из окружения и настроек ОС. Живой прогон Э7
            # показал, что на машине с системным прокси локальный запрос получал
            # `503` от прокси и уведомления до кассы не доходили — та же ловушка,
            # что закрыта в `AgentClient` (см. `TestProxyIsIgnored`)
            trust_env=False,
        )

    # --- Чтение -------------------------------------------------------------

    def pending(self, ref: SessionRef) -> list[NotificationPendingItem]:
        """`GET /notifications/pending` — что показать в этой сессии (§5.1.2).

        Ответ службы — уже отфильтрованный: закрытое, отложенное до срока и
        просроченное в списке не приходит (`notification_queue.pending_for`).
        """
        response = self._request(
            "GET",
            "/notifications/pending",
            params={"session": ref.session_id, "user": ref.session_user},
        )
        payload = response.json() or {}
        raw_items: Sequence[dict] = payload.get("notifications", [])
        return [NotificationPendingItem.model_validate(item) for item in raw_items]

    # --- Действия пользователя ---------------------------------------------

    def shown(self, notification_id: int, ref: SessionRef) -> bool:
        """`POST /notifications/{id}/shown` — тост `info` показан (§7.4)."""
        return self._post(f"/notifications/{notification_id}/shown", ref.model_dump())

    def ack(self, notification_id: int, ref: SessionRef, *, comment: str | None = None) -> bool:
        """`POST /notifications/{id}/ack` — кнопка «Ознакомлен» (FR-26)."""
        body = ref.model_dump()
        if comment:
            body["comment"] = comment
        return self._post(f"/notifications/{notification_id}/ack", body)

    def snooze(self, notification_id: int, ref: SessionRef, *, snooze_min: int | None = None) -> bool:
        """`POST /notifications/{id}/snooze` — кнопка «Отложить» (П-2, Д-10).

        Интервал возврата — настройка сервера; трей его не задаёт и передаёт
        только если сервер прислал значение в самом уведомлении.
        """
        body = ref.model_dump()
        if snooze_min is not None:
            body["snooze_min"] = snooze_min
        return self._post(f"/notifications/{notification_id}/snooze", body)

    # --- Внутреннее ---------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Выполнить запрос к локальной службе; сетевые сбои → `AgentUnreachable`."""
        try:
            response = self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AgentUnreachable(f"Локальная служба агента недоступна: {exc}") from exc
        return response

    def _post(self, url: str, body: dict) -> bool:
        """Отправка подтверждения: `True` — принято, `False` — служба не ответила.

        Уведомление при `False` остаётся видимым: окно закроется только после
        успешного `ack`/`snooze`, поэтому подтверждение не теряется.
        """
        try:
            response = self._client.post(url, json=body)
            response.raise_for_status()
        except httpx.HTTPError:
            return False
        return True

    def close(self) -> None:
        """Закрыть HTTP-соединения (вызывается при выходе из трея)."""
        self._client.close()

    def __enter__(self) -> "TrayAgentClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
