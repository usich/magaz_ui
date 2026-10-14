"""Магазины (§4.1) и учётные данные 1С (§8.3, П-1) — сервисный слой.

Состав этапа Э2: CRUD по колонкам ТЗ, генерация и перевыпуск `agent_token`,
хранение логина и пароля 1С (Fernet) с инкрементом `credentials_version`.
Пароль наружу не отдаётся никогда: API и HTML получают только признак
`Store.password_set` («задан»), значение живёт в `stores.password_enc` (§8.3, I-7).
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.core.security import SecretBox, generate_agent_token
from server.db.models import Store
from shared.paths import validate_ipv4_address
from shared.timeutil import utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_AGENT_PORT",
    "STATUS_ALL",
    "STATUS_OFFLINE",
    "STATUS_ONLINE",
    "StoreNotFoundError",
    "StoreValidationError",
    "count_by_status",
    "create_store",
    "deactivate_store",
    "get_store",
    "list_stores",
    "rotate_agent_token",
    "set_credentials",
    "update_store",
]

#: Порт агента по умолчанию (§4.1, Приложение B)
DEFAULT_AGENT_PORT = 8383

#: Значения фильтра «статус» в списке магазинов (§11.2)
STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"
STATUS_ALL = "all"


class StoreValidationError(ValueError):
    """Данные магазина не проходят проверку (дубликат имени/IP, пустое поле)."""


class StoreNotFoundError(LookupError):
    """Магазин с указанным id не найден."""


def _require_text(value: str | None, *, field: str) -> str:
    """Непустое текстовое поле (иначе — понятная ошибка валидации)."""
    raw = (value or "").strip()
    if not raw:
        raise StoreValidationError(f"Поле «{field}» не может быть пустым")
    return raw


def _validate_port(value: int) -> int:
    """Порт агента в допустимом диапазоне."""
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise StoreValidationError("Порт агента должен быть целым числом") from exc
    if not 1 <= port <= 65535:
        raise StoreValidationError("Порт агента должен быть в диапазоне 1…65535")
    return port


def _validate_ip(value: str) -> str:
    """IP кассы в туннеле — только IPv4 (§4.1)."""
    try:
        return validate_ipv4_address(value)
    except ValueError as exc:
        raise StoreValidationError(str(exc)) from exc


def _ensure_unique(
    session: Session,
    *,
    name: str | None = None,
    ip_address: str | None = None,
    exclude_id: int | None = None,
) -> None:
    """Проверить уникальность имени и IP **среди активных** магазинов (§4.1, §5.2.2).

    Отключённый магазин (`is_active=false`) не занимает ни имя, ни IP: «Отключить» —
    мягкое удаление (§5.2.2), поэтому кассу можно завести заново, не теряя историю
    задач и уведомлений по старой записи. То же правило закреплено в БД частичными
    уникальными индексами `uq_stores_name_active` / `uq_stores_ip_address_active`
    (`server/db/models.py`).

    Сравнение имён идёт в Python (`str.casefold`), а не через SQL `lower()`:
    SQLite приводит регистр только для латиницы, поэтому «Магазин №5» и
    «магазин №5» через SQL считались бы разными.
    """
    if name:
        wanted = name.strip().casefold()
        stmt = select(Store.id, Store.name).where(Store.is_active.is_(True))
        for row_id, row_name in session.execute(stmt).all():
            if row_id == exclude_id:
                continue
            if (row_name or "").strip().casefold() == wanted:
                raise StoreValidationError(
                    f"Магазин с именем {name!r} уже существует (занято активным магазином)"
                )

    if ip_address:
        stmt = select(Store.id).where(
            Store.ip_address == ip_address, Store.is_active.is_(True)
        )
        if exclude_id is not None:
            stmt = stmt.where(Store.id != exclude_id)
        if session.scalars(stmt).first() is not None:
            raise StoreValidationError(
                f"Магазин с IP {ip_address} уже существует (занят активным магазином)"
            )


def _flush_or_conflict(session: Session) -> None:
    """`flush` с понятной ошибкой, если уникальность всё-таки нарушена в БД.

    Обычный путь закрыт проверками `_ensure_unique`; сюда попадаем при гонке двух
    администраторов — тогда срабатывает частичный уникальный индекс, и вместо `500`
    администратор видит тот же понятный текст, что и при обычной проверке.
    """
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise StoreValidationError(
            "Имя или IP уже заняты активным магазином — обновите список и повторите"
        ) from exc


def get_store(session: Session, store_id: int) -> Store:
    """Магазин по id или `StoreNotFoundError` (для API это `404`)."""
    store = session.get(Store, store_id)
    if store is None:
        raise StoreNotFoundError(f"Магазин id={store_id} не найден")
    return store


def create_store(
    session: Session,
    *,
    name: str,
    ip_address: str,
    cluster_name: str,
    database_name: str,
    version_1c: str,
    user_name_1c: str,
    agent_port: int = DEFAULT_AGENT_PORT,
    password: str | None = None,
    secret_box: SecretBox | None = None,
    comment: str | None = None,
) -> Store:
    """Создать магазин и выдать ему токен агента (§11.2).

    Токен генерирует **только** сервер: он попадает в блок «что вписать в конфиг
    агента» и больше нигде не показывается в открытом виде.
    """
    normalized_name = _require_text(name, field="Название магазина")
    normalized_ip = _validate_ip(_require_text(ip_address, field="IP-адрес"))
    _ensure_unique(session, name=normalized_name, ip_address=normalized_ip)

    store = Store(
        name=normalized_name,
        ip_address=normalized_ip,
        agent_port=_validate_port(agent_port),
        cluster_name=_require_text(cluster_name, field="Кластер (сервер 1С)"),
        database_name=_require_text(database_name, field="Имя базы 1С"),
        version_1c=_require_text(version_1c, field="Версия 1С"),
        user_name_1c=_require_text(user_name_1c, field="Пользователь 1С"),
        agent_token=generate_agent_token(),
        credentials_version=1,
        devices_version=1,
        is_active=True,
        comment=(comment or "").strip() or None,
    )
    if password:
        if secret_box is None:  # pragma: no cover - защита от неверного вызова
            raise StoreValidationError("Не передан шифровальщик для пароля 1С")
        store.password_enc = secret_box.encrypt(password)

    session.add(store)
    _flush_or_conflict(session)
    logger.info("Создан магазин id=%s (%s, %s)", store.id, store.name, store.ip_address)
    return store


def update_store(
    session: Session,
    store: Store,
    *,
    name: str | None = None,
    ip_address: str | None = None,
    cluster_name: str | None = None,
    database_name: str | None = None,
    version_1c: str | None = None,
    user_name_1c: str | None = None,
    agent_port: int | None = None,
    comment: str | None = None,
    is_active: bool | None = None,
) -> Store:
    """Изменить реквизиты магазина (передаются только изменяемые поля).

    Смена логина 1С увеличивает `credentials_version` (§4.1, П-1): агент увидит
    расхождение в `/health` и получит новые учётные данные push-ом (Э4).
    """
    if name is not None:
        normalized_name = _require_text(name, field="Название магазина")
        _ensure_unique(session, name=normalized_name, exclude_id=store.id)
        store.name = normalized_name

    if ip_address is not None:
        normalized_ip = _validate_ip(_require_text(ip_address, field="IP-адрес"))
        _ensure_unique(session, ip_address=normalized_ip, exclude_id=store.id)
        store.ip_address = normalized_ip

    if cluster_name is not None:
        store.cluster_name = _require_text(cluster_name, field="Кластер (сервер 1С)")
    if database_name is not None:
        store.database_name = _require_text(database_name, field="Имя базы 1С")
    if version_1c is not None:
        store.version_1c = _require_text(version_1c, field="Версия 1С")
    if agent_port is not None:
        store.agent_port = _validate_port(agent_port)
    if comment is not None:
        store.comment = comment.strip() or None
    if is_active is not None:
        if is_active and not store.is_active:
            # Включение обратно (§5.2.2): пока магазин был отключён, его имя или IP
            # мог занять другой активный магазин. Проверяем **до** записи, чтобы при
            # отказе (`422`) запись магазина осталась нетронутой — неактивной.
            _ensure_unique(
                session,
                name=store.name,
                ip_address=store.ip_address,
                exclude_id=store.id,
            )
        store.is_active = bool(is_active)

    if user_name_1c is not None:
        normalized_user = _require_text(user_name_1c, field="Пользователь 1С")
        if normalized_user != store.user_name_1c:
            store.user_name_1c = normalized_user
            store.credentials_version += 1

    _flush_or_conflict(session)
    return store


def set_credentials(
    session: Session,
    store: Store,
    *,
    user_name_1c: str | None = None,
    password: str | None = None,
    clear_password: bool = False,
    secret_box: SecretBox | None = None,
) -> Store:
    """Задать/заменить/очистить учётные данные 1С (П-1, §8.3).

    Значение пароля только шифруется: в лог и аудит уходит факт изменения, а не
    само значение (I-7). Версия растёт всегда — это сигнал агенту «перезапроси».
    """
    if user_name_1c is not None:
        store.user_name_1c = _require_text(user_name_1c, field="Пользователь 1С")

    if clear_password:
        store.password_enc = None
    elif password:
        if secret_box is None:  # pragma: no cover - защита от неверного вызова
            raise StoreValidationError("Не передан шифровальщик для пароля 1С")
        store.password_enc = secret_box.encrypt(password)

    store.credentials_version += 1
    session.flush()
    logger.info(
        "Учётные данные 1С магазина id=%s обновлены (версия %s, пароль %s)",
        store.id,
        store.credentials_version,
        "задан" if store.password_set else "не задан",
    )
    return store


def rotate_agent_token(session: Session, store: Store) -> str:
    """Перевыпустить `agent_token` (§11.2: агент потребуется перенастроить).

    Значение токена в лог не пишется — только факт ротации.
    """
    store.agent_token = generate_agent_token()
    session.flush()
    logger.info("Токен агента магазина id=%s перевыпущен", store.id)
    return store.agent_token


def deactivate_store(
    session: Session, store: Store, *, comment: str | None = None
) -> Store:
    """Мягкое удаление (§5.2.2): `is_active=false` — исключение из мониторинга.

    Имя и IP при этом освобождаются: уникальность считается только среди активных
    магазинов (§4.1), поэтому кассу можно завести заново — история задач и
    уведомлений остаётся у этой записи.
    """
    store.is_active = False
    if comment:
        store.comment = comment.strip() or store.comment
    session.flush()
    logger.info("Магазин id=%s переведён в неактивные", store.id)
    return store


def list_stores(
    session: Session,
    *,
    search: str | None = None,
    cluster: str | None = None,
    status: str | None = None,
    include_inactive: bool = False,
    now: datetime | None = None,
    online_window_sec: int = 45,
) -> list[Store]:
    """Список магазинов с фильтрами страницы `/stores` (§11.2).

    Фильтр по статусу считается на лету по `last_seen_at` (§3.3) — «онлайн» не
    хранится в БД, поэтому не может разойтись с фактом.
    """
    stmt = select(Store)
    if not include_inactive:
        stmt = stmt.where(Store.is_active.is_(True))
    if cluster:
        stmt = stmt.where(Store.cluster_name == cluster.strip())

    stores = list(session.scalars(stmt.order_by(Store.name)).all())

    # Поиск — в Python (`casefold`): SQL `lower()` в SQLite не понимает кириллицу,
    # а магазинов десятки (§2 Q7), поэтому стоимость пренебрежима
    query = (search or "").strip().casefold()
    if query:
        stores = [
            store
            for store in stores
            if query
            in " ".join(
                (
                    store.name or "",
                    store.ip_address or "",
                    store.database_name or "",
                    store.cluster_name or "",
                )
            ).casefold()
        ]

    normalized_status = (status or STATUS_ALL).strip().lower()
    if normalized_status == STATUS_ONLINE:
        return [store for store in stores if store.is_online(now, online_window_sec)]
    if normalized_status == STATUS_OFFLINE:
        return [store for store in stores if not store.is_online(now, online_window_sec)]
    return stores


def count_by_status(
    session: Session, *, now: datetime | None = None, online_window_sec: int = 45
) -> tuple[int, int]:
    """Счётчики «Онлайн / Офлайн» для плиток главной (§11.1, FR-13).

    Учитываются только активные магазины: неактивные исключены из мониторинга.
    """
    stores = list(
        session.scalars(select(Store).where(Store.is_active.is_(True))).all()
    )
    reference = now or utcnow()
    online = sum(1 for store in stores if store.is_online(reference, online_window_sec))
    return online, len(stores) - online
