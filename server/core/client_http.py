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
from shared.schemas import (
    CheckFilesResponse,
    CredentialsPush,
    DeleteFileResponse,
    DeviceCheckRequest,
    DeviceCheckResult,
    DevicesConfigPush,
    HealthResponse,
    MergeParams,
    NotifyRequest,
    UpdateParams,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AgentAuthError",
    "AgentClient",
    "AgentError",
    "AgentOfflineError",
    "AgentProtocolError",
    "AgentTarget",
    "JOB_STATE_PATHS",
    "build_agent_target",
]

#: Схема по умолчанию: внутри туннеля HTTPS не нужен (§8.2, Q35)
DEFAULT_AGENT_SCHEME = "http"
#: Порт агента по умолчанию (§4.1, Приложение B)
DEFAULT_AGENT_PORT = 8383

HEALTH_PATH = "/health"
VERSION_PATH = "/version"
#: §5.1.1: перенос v2 — эти пути и их коды менять нельзя (Приложение B)
CHECK_FILES_PATH = "/check-files"
RUN_MERGE_PATH = "/run-merge"
RUN_UPDATE_DB_PATH = "/run-update-db"
DOWNLOAD_FILE_PATH = "/download-file"
SEND_MESSAGE_PATH = "/send-message"
#: §5.1.2: push учётных данных 1С (П-1)
CREDENTIALS_PATH = "/credentials"
#: §5.1.2: список оборудования подсети (Д-1) и внеочередная проверка (кнопка)
DEVICES_CONFIG_PATH = "/devices/config"
DEVICES_CHECK_NOW_PATH = "/devices/check-now"
#: §5.1.2: приём уведомления от сервера (ФН3, Э5)
NOTIFY_PATH = "/notify"
#: Э10: удаление файлов обновления в рабочей папке кассы
DELETE_FILES_PATH = "/files/delete"

#: Тип задачи → путь опроса его состояния (`/check-*`, Приложение B)
JOB_STATE_PATHS: dict[str, str] = {
    "run_merge": "/check-merge",
    "run_update_db": "/check-update-db",
}



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
        query: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Выполнить запрос к агенту; сетевые сбои → `AgentOfflineError`.

        Статус здесь не проверяется: вызывающий сам решает, что значат `202`/`400`
        (Э4 использует статус `/check-*` как результат операции).
        """
        try:
            return await self.client.request(method, path, json=json_body, params=query)
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

    # --- Эндпоинты §5.1.1 (перенос v2): их вызывает диспетчер (Э4) ----------

    async def check_files(self, filename: str) -> CheckFilesResponse:
        """`POST /check-files` — наличие `.cf/.cfe/.xml` и их размеры (Q52)."""
        body = await self.request_json("POST", CHECK_FILES_PATH, json_body={"filename": filename})
        try:
            return CheckFilesResponse.model_validate(body)
        except ValidationError as exc:
            raise AgentProtocolError(
                "Агент магазина id="
                f"{self.target.store_id} вернул неожиданный /check-files ({exc.error_count()} ошибок)"
            ) from exc

    async def run_merge(self, params: MergeParams | dict[str, Any]) -> dict[str, Any]:
        """`POST /run-merge` — поставить слияние (перенос v2, ответ `202`)."""
        return await self._accepted(RUN_MERGE_PATH, params)

    async def run_update_db(self, params: UpdateParams | dict[str, Any]) -> dict[str, Any]:
        """`POST /run-update-db` — поставить обновление БД (перенос v2, ответ `202`)."""
        return await self._accepted(RUN_UPDATE_DB_PATH, params)

    async def download_file(self, url_file: str) -> dict[str, Any]:
        """`POST /download-file` — скачать пакет по ссылке (ФН2, FR-1)."""
        return await self._accepted(DOWNLOAD_FILE_PATH, {"url-file": url_file})

    async def send_message(self, text_message: str) -> dict[str, Any]:
        """`POST /send-message` — окно `msg` + завершение `1cv8.exe` (FR-8)."""
        return await self._accepted(SEND_MESSAGE_PATH, {"text_message": text_message})

    async def delete_files(self, file_name: str, *, suffix: str | None = None) -> DeleteFileResponse:
        """`POST /files/delete` — удалить файлы обновления на кассе (Э10, §11.3).

        Ответ разбирается строго: диспетчер показывает в журнале задачи, что именно
        удалено, а чего на кассе не было.
        """
        payload: dict[str, Any] = {"file_name": file_name}
        if suffix:
            payload["suffix"] = suffix
        body = await self.request_json("POST", DELETE_FILES_PATH, json_body=payload)
        try:
            return DeleteFileResponse.model_validate(body)
        except ValidationError as exc:
            raise AgentProtocolError(
                "Агент магазина id="
                f"{self.target.store_id} вернул неожиданный /files/delete ({exc.error_count()} ошибок)"
            ) from exc

    async def check_job_state(
        self, kind: str, *, extension: bool = False
    ) -> tuple[int, dict[str, Any]]:
        """`GET /check-merge` / `GET /check-update-db` — статус операции (FR-7).

        Возвращает пару «код ответа + тело», потому что по Приложению B код и
        есть результат: `202` — идёт, `200` — успех, `400` — ошибка.
        """
        if kind not in JOB_STATE_PATHS:
            raise AgentProtocolError(f"Неизвестный тип задачи для опроса: {kind!r}")

        path = JOB_STATE_PATHS[kind]
        response = await self.request(
            "GET", path, query={"extension": str(bool(extension)).lower()}
        )
        status_code = response.status_code
        if status_code in (401, 403):
            self._raise_for_status(response)
        if status_code not in (200, 202, 400):
            self._raise_for_status(response)
        try:
            body = response.json()
        except ValueError as exc:
            raise AgentProtocolError(
                f"Агент магазина id={self.target.store_id} вернул не-JSON в {path}",
                status_code=status_code,
            ) from exc
        if not isinstance(body, dict):
            raise AgentProtocolError(
                f"Агент магазина id={self.target.store_id} вернул не объект в {path}",
                status_code=status_code,
            )
        return status_code, body

    async def push_credentials(self, payload: CredentialsPush) -> dict[str, Any]:
        """`POST /credentials` — доставить учётные данные 1С на кассу (П-1, §8.3).

        Значение пароля живёт только в теле запроса: ни в лог, ни в БД оно не
        попадает (I-7).
        """
        body = await self.request_json(
            "POST", CREDENTIALS_PATH, json_body=payload.model_dump()
        )
        if not isinstance(body, dict):
            raise AgentProtocolError(
                f"Агент магазина id={self.target.store_id} вернул не объект в /credentials"
            )
        return body

    async def _accepted(self, path: str, payload: Any) -> dict[str, Any]:  # noqa: ANN401
        """Общий разбор ответа `/run-*`: `202` + JSON-подтверждение."""
        body = await self.request_json("POST", path, json_body=payload)
        return body if isinstance(body, dict) else {}

    async def notify(self, payload: NotifyRequest) -> dict[str, Any]:
        """`POST /notify` — доставить уведомление на кассу (ФН3, §7.4).

        Агент кладёт уведомление в файловую очередь (`notifications.json`),
        поэтому недоступность сервера не важна: окно появится, когда служба
        поднимется. В ответе приходит число сессий, которые касса сейчас
        видит, — оно идёт в `notification_targets.sessions_count` (§4.7).
        """
        body = await self.request_json(
            "POST", NOTIFY_PATH, json_body=payload.model_dump(mode="json")
        )
        if not isinstance(body, dict):
            raise AgentProtocolError(
                f"Агент магазина id={self.target.store_id} вернул не объект в /notify"
            )
        return body

    async def push_devices_config(self, payload: DevicesConfigPush) -> dict[str, Any]:
        """`POST /devices/config` — доставить список оборудования на кассу (Д-1).

        Вместе со списком едет расписание опроса из настроек сервера (§4.9, Д-9),
        поэтому интервал меняется в UI без правки конфига на каждой кассе.
        """
        body = await self.request_json(
            "POST", DEVICES_CONFIG_PATH, json_body=payload.model_dump(mode="json")
        )
        if not isinstance(body, dict):
            raise AgentProtocolError(
                f"Агент магазина id={self.target.store_id} вернул не объект в /devices/config"
            )
        return body

    async def check_devices_now(
        self, payload: DeviceCheckRequest
    ) -> list[DeviceCheckResult]:
        """`POST /devices/check-now` — внеочередная проверка устройств (Д-1).

        Адреса передаёт сервер (только из `store_devices`), ответ синхронный:
        агент опрашивает подсеть и сразу возвращает статусы и задержки.
        """
        body = await self.request_json(
            "POST", DEVICES_CHECK_NOW_PATH, json_body=payload.model_dump(mode="json")
        )
        if not isinstance(body, list):
            raise AgentProtocolError(
                f"Агент магазина id={self.target.store_id} вернул не список в /devices/check-now"
            )
        try:
            return [DeviceCheckResult.model_validate(item) for item in body]
        except ValidationError as exc:
            raise AgentProtocolError(
                "Агент магазина id="
                f"{self.target.store_id} вернул неожиданный /devices/check-now "
                f"({exc.error_count()} ошибок)"
            ) from exc

        return body if isinstance(body, dict) else {}

