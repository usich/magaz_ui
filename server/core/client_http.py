"""Единая точка исходящих вызовов сервера к агентам (§6, инвариант I-11).

Сервер разговаривает с кассами по локальным IP внутри WireGuard-туннеля (Q1):
HTTP, заголовок `X-Agent-Token`, таймаут на каждый запрос. Все исходящие вызовы
идут через этот модуль — так настройки (схема, таймаут, транспорт) не «зашиты»
в бизнес-код и подменяются в тестах одной строкой.

Состав этапа Э2:
* `AgentTarget` — куда и с каким токеном обращаться (собирается из `stores`);
* `AgentClient` — асинхронный httpx-клиент: `get_health()`, `get_version()`,
  а также низкоуровневый `request()`, на который опирается Э4 (диспетчер);
* типизированные ошибки: агент недоступен, агент отклонил токен, ответ нельзя
  использовать. Мониторинг и карточка магазина по типу ошибки выбирают
  `503`/`502`, а не «ошибка 500».
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import ValidationError

from server.config import Settings
from shared.schemas import HealthResponse

logger = logging.getLogger(__name__)

__all__ = [
    "AgentAuthError",
    "AgentClient",
    "AgentError",
    "AgentOfflineError",
    "AgentProtocolError",
    "AgentTarget",
    "build_agent_target",
]

#: Схема по умолчанию: внутри туннеля HTTPS не нужен (§8.2, Q35)
DEFAULT_AGENT_SCHEME = "http"
#: Порт агента по умолчанию (§4.1, Приложение B)
DEFAULT_AGENT_PORT = 8383

HEALTH_PATH = "/health"
VERSION_PATH = "/version"


class AgentError(Exception):
    """Базовая ошибка обращения к агенту."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class AgentOfflineError(AgentError):
    """Агент не ответил: нет соединения, таймаут, обрыв связи."""


class AgentAuthError(AgentError):
    """Агент отклонил запрос: неверный `X-Agent-Token` (`401`) или чужой IP (`403`)."""


class AgentProtocolError(AgentError):
    """Ответ получен, но использовать его нельзя: не-2xx, битый JSON, чужая схема."""


@dataclass(slots=True)
class AgentTarget:
    """Куда обращаться и чем подтверждаться (магазин → адрес агента)."""

    store_id: int
    base_url: str
    token: str
    _headers: dict[str, str] = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self._headers = {"X-Agent-Token": self.token, "Accept": "application/json"}

    @property
    def headers(self) -> dict[str, str]:
        """Заголовки запроса к агенту (токен магазина, §8.1)."""
        return dict(self._headers)


def build_agent_target(store: Any, settings: Settings) -> AgentTarget:  # noqa: ANN401 - ORM Store
    """Собрать цель вызова из строки `stores` и настроек сервера."""
    scheme = (settings.agent_scheme or DEFAULT_AGENT_SCHEME).strip() or DEFAULT_AGENT_SCHEME
    port = int(getattr(store, "agent_port", DEFAULT_AGENT_PORT) or DEFAULT_AGENT_PORT)
    return AgentTarget(
        store_id=int(store.id),
        base_url=f"{scheme}://{store.ip_address}:{port}",
        token=store.agent_token,
    )


class AgentClient:
    """Асинхронный клиент агента: токен, таймаут, инжектируемый транспорт.

    `transport` нужен тестам: `httpx.MockTransport` подменяет сеть, поэтому
    проверки мониторинга не зависят ни от кассы, ни от туннеля (§13).
    """

    def __init__(
        self,
        target: AgentTarget,
        *,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.target = target
        self.timeout = float(timeout)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    # --- Жизненный цикл -----------------------------------------------------

    @property
    def client(self) -> httpx.AsyncClient:
        """Лениво созданный httpx-клиент (I-10: ничего на импорте)."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.target.base_url,
                timeout=httpx.Timeout(self.timeout),
                headers=self.target.headers,
                transport=self._transport,
                # Прокси не должен вмешиваться: агенты живут на приватных адресах
                # туннеля, а httpx по умолчанию берёт прокси из окружения и настроек
                # ОС — на рабочей машине это перехватывало обращения к кассе
                # (см. регресс-тест `TestProxyIsIgnored`)
                trust_env=False,
            )
        return self._client

    async def __aenter__(self) -> "AgentClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Закрыть соединения (идемпотентно)."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- Запросы ------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,  # noqa: ANN401 - тело запроса к агенту
    ) -> httpx.Response:
        """Выполнить запрос к агенту; сетевые сбои → `AgentOfflineError`.

        Статус здесь не проверяется: вызывающий сам решает, что значат `202`/`400`
        (Э4 использует статус `/check-*` как результат операции).
        """
        try:
            return await self.client.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            logger.debug(
                "Агент магазина id=%s недоступен (%s %s): %s",
                self.target.store_id,
                method,
                path,
                exc.__class__.__name__,
            )
            raise AgentOfflineError(
                f"Агент магазина id={self.target.store_id} недоступен: {exc.__class__.__name__}"
            ) from exc

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,  # noqa: ANN401 - тело запроса к агенту
    ) -> Any:  # noqa: ANN401 - разобранное тело ответа
        """Запрос с проверкой статуса и разбором JSON."""
        response = await self.request(method, path, json_body=json_body)
        self._raise_for_status(response)
        try:
            return response.json()
        except ValueError as exc:
            raise AgentProtocolError(
                f"Агент магазина id={self.target.store_id} вернул не-JSON ответ",
                status_code=response.status_code,
            ) from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        """Превратить статус агента в нашу ошибку (§5.1.2, Приложение B)."""
        status_code = response.status_code
        if status_code in (401, 403):
            raise AgentAuthError(
                "Агент отклонил запрос: проверьте токен магазина и IP источника",
                status_code=status_code,
            )
        if status_code >= 400:
            raise AgentProtocolError(
                f"Агент ответил статусом {status_code}",
                status_code=status_code,
            )

    # --- Эндпоинты §5.1.2 ---------------------------------------------------

    async def get_health(self) -> HealthResponse:
        """`GET /health` — мониторинг и идентификация магазина (§5.1.2)."""
        body = await self.request_json("GET", HEALTH_PATH)
        try:
            return HealthResponse.model_validate(body)
        except ValidationError as exc:
            raise AgentProtocolError(
                "Агент магазина id="
                f"{self.target.store_id} вернул неожиданный /health ({exc.error_count()} ошибок)"
            ) from exc

    async def get_version(self) -> dict[str, Any]:
        """`GET /version` — версия агента и рабочие пути (§5.1.2)."""
        body = await self.request_json("GET", VERSION_PATH)
        if not isinstance(body, dict):
            raise AgentProtocolError(
                f"Агент магазина id={self.target.store_id} вернул не объект в /version"
            )
        return body
