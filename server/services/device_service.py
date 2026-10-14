"""Оборудование подсети магазина (§4.11, Д-1) — сервисный слой.

Сервер подсеть магазина **не пингует** (§3.3): список устройств ведёт админ, а
опрос выполняет агент — он физически внутри подсети. Отсюда две задачи сервиса:

* CRUD списка (`store_devices`) и версия списка `stores.devices_version`:
  любое изменение инкрементирует её, и по расхождению версий диспетчер
  досылает список агенту push-ом `POST /devices/config` (§7.6, Д-2);
* приём результата опроса батчем `POST /api/v1/agent/devices/status`:
  `online`/`offline`/`unknown`, задержка и текст последней ошибки (§4.11).

«Онлайн» не хранится как истина: `StoreDevice.is_online()` считает его по
`last_status` и `last_seen_at` с окном `device_online_window_sec` — так статус
не может разойтись с фактом. Результаты по адресам, которых нет в списке
магазина, игнорируются: агент не сканирует подсеть (§7.6), а батч не должен
превратиться в способ «протащить» чужие адреса.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.db.models import Store, StoreDevice
from server.services import settings_service
from shared.enums import CheckType, DeviceStatus, DeviceType
from shared.paths import validate_ipv4_address
from shared.schemas import DeviceSpec, DeviceStatusBatch, DevicesConfigPush
from shared.timeutil import utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "CHECK_TYPES",
    "DEVICE_STATUSES",
    "DEVICE_TYPES",
    "STATUS_ALL",
    "STATUS_FILTERS",
    "DeviceNotFoundError",
    "DeviceValidationError",
    "apply_status_batch",
    "bump_devices_version",
    "count_devices",
    "create_device",
    "deactivate_device",
    "devices_window_sec",
    "get_device",
    "list_devices",
    "list_devices_for_store",
    "push_payload",
    "update_device",
]

#: §4.11: допустимые типы устройств и способы проверки
DEVICE_TYPES: tuple[str, ...] = tuple(item.value for item in DeviceType)
CHECK_TYPES: tuple[str, ...] = tuple(item.value for item in CheckType)
DEVICE_STATUSES: tuple[str, ...] = tuple(item.value for item in DeviceStatus)

#: Значения фильтра «статус» на странице `/devices` (§11.7)
STATUS_ALL = "all"
STATUS_FILTERS: tuple[str, ...] = (STATUS_ALL, *DEVICE_STATUSES)

#: §4.9: настройки расписания, которые уезжают агенту вместе со списком (Д-9)
INTERVAL_SETTING = "device_check_interval_sec"
TIMEOUT_SETTING = "device_ping_timeout_ms"
WINDOW_SETTING = "device_online_window_sec"

#: Пределы из спецификации: значения проверяются и здесь, и в pydantic-схемах
MAX_NAME_LENGTH = 200
MAX_TEXT_LENGTH = 4000


class DeviceValidationError(ValueError):
    """Данные устройства не проходят проверку (дубликат IP, пустое поле)."""


class DeviceNotFoundError(LookupError):
    """Устройство с указанным id не найдено у магазина."""


def _require_text(value: str | None, *, field: str) -> str:
    """Непустое текстовое поле (иначе — понятная ошибка валидации)."""
    raw = (value or "").strip()
    if not raw:
        raise DeviceValidationError(f"Поле «{field}» не может быть пустым")
    return raw


def _validate_ip(value: str) -> str:
    """IP устройства в подсети магазина — только явный IPv4 (I-15)."""
    try:
        return validate_ipv4_address(value)
    except ValueError as exc:
        raise DeviceValidationError(str(exc)) from exc


def _validate_type(value: str | None) -> str | None:
    """Тип устройства: `camera` / `switch` / `printer` / `pc` / `other` (§4.11)."""
    raw = (value or "").strip().lower()
    if not raw:
        return None
    if raw not in DEVICE_TYPES:
        raise DeviceValidationError(
            f"Недопустимый тип устройства: {value!r} (ожидается {', '.join(DEVICE_TYPES)})"
        )
    return raw


def _validate_check(check_type: str | None, port: int | None) -> tuple[str, int | None]:
    """Способ проверки и порт: `tcp` без порта не имеет смысла (§4.11)."""
    raw = (check_type or CheckType.ICMP.value).strip().lower()
    if raw not in CHECK_TYPES:
        raise DeviceValidationError(
            f"Недопустимый способ проверки: {check_type!r} (ожидается icmp или tcp)"
        )
    if raw == CheckType.ICMP.value:
        return raw, None
    if port is None:
        raise DeviceValidationError("Для способа проверки tcp обязателен порт")
    try:
        number = int(port)
    except (TypeError, ValueError) as exc:
        raise DeviceValidationError("Порт проверки должен быть целым числом") from exc
    if not 1 <= number <= 65535:
        raise DeviceValidationError("Порт проверки должен быть в диапазоне 1…65535")
    return raw, number


# --- Чтение ----------------------------------------------------------------


def _settings(session: Session) -> tuple[int, int, int]:
    """Расписание и окно «онлайн» из настроек §4.9 (Д-9)."""
    interval = max(1, settings_service.get_int(session, INTERVAL_SETTING))
    timeout = max(1, settings_service.get_int(session, TIMEOUT_SETTING))
    window = max(1, settings_service.get_int(session, WINDOW_SETTING))
    return interval, timeout, window


def devices_window_sec(session: Session) -> int:
    """Окно, в котором устройство считается онлайн (§4.11, `device_online_window_sec`)."""
    return _settings(session)[2]


def get_device(session: Session, store_id: int, device_id: int) -> StoreDevice:
    """Устройство магазина по id; чужой id — `DeviceNotFoundError` (§5.2.2)."""
    device = session.get(StoreDevice, device_id)
    if device is None or device.store_id != store_id:
        raise DeviceNotFoundError(f"Устройство id={device_id} не найдено у магазина id={store_id}")
    return device


def list_devices_for_store(
    session: Session,
    store_id: int,
    *,
    include_inactive: bool = True,
) -> list[StoreDevice]:
    """Все устройства магазина по имени (для карточки магазина, §11.2)."""
    stmt = select(StoreDevice).where(StoreDevice.store_id == store_id)
    if not include_inactive:
        stmt = stmt.where(StoreDevice.is_active.is_(True))
    return list(session.scalars(stmt.order_by(StoreDevice.name)).all())


def list_devices(
    session: Session,
    *,
    store_id: int | None = None,
    cluster: str | None = None,
    device_type: str | None = None,
    status: str | None = None,
    search: str | None = None,
    include_inactive: bool = True,
    include_inactive_stores: bool = False,
    now: datetime | None = None,
    window_sec: int | None = None,
) -> list[StoreDevice]:
    """Устройства с фильтрами страницы `/devices` (§11.7).

    Фильтр по статусу считается в Python по тому же правилу, что и в UI:
    «онлайн» — успешный ответ за окно, «офлайн» — неудачная проверка,
    «unknown» — данных нет (например, касса молчит, §4.11).
    """
    stmt = select(StoreDevice).join(Store, Store.id == StoreDevice.store_id)
    if store_id is not None:
        stmt = stmt.where(StoreDevice.store_id == store_id)
    if cluster:
        stmt = stmt.where(Store.cluster_name == cluster.strip())
    if not include_inactive_stores:
        stmt = stmt.where(Store.is_active.is_(True))
    if not include_inactive:
        stmt = stmt.where(StoreDevice.is_active.is_(True))

    devices = list(session.scalars(stmt.order_by(Store.name, StoreDevice.name)).all())

    normalized_type = (device_type or "").strip().lower()
    if normalized_type and normalized_type not in DEVICE_TYPES:
        raise DeviceValidationError(f"Недопустимый тип устройства: {device_type!r}")
    if normalized_type:
        devices = [item for item in devices if (item.device_type or "") == normalized_type]

    window = window_sec if window_sec is not None else _settings(session)[2]
    reference = now or utcnow()
    wanted = (status or STATUS_ALL).strip().lower()
    if wanted not in STATUS_FILTERS:
        raise DeviceValidationError(f"Недопустимый статус фильтра: {status!r}")
    if wanted == DeviceStatus.ONLINE.value:
        return [item for item in devices if item.is_online(reference, window)]
    if wanted == DeviceStatus.OFFLINE.value:
        return [item for item in devices if item.last_status == DeviceStatus.OFFLINE.value]
    if wanted == DeviceStatus.UNKNOWN.value:
        return [item for item in devices if item.last_status in (None, DeviceStatus.UNKNOWN.value)]

    # Поиск — в Python (`casefold`): SQL `lower()` в SQLite не понимает кириллицу,
    # а устройств у 35–40 магазинов немного (§2)
    query = (search or "").strip().casefold()
    if query:
        devices = [
            item
            for item in devices
            if query
            in " ".join(
                (
                    item.name or "",
                    item.ip_address or "",
                    item.device_type or "",
                    item.comment or "",
                    item.store.name if item.store is not None else "",
                )
            ).casefold()
        ]
    return devices



# --- CRUD (§5.2.2, §11.7) ---------------------------------------------------


def _ensure_unique_ip(
    session: Session, *, store_id: int, ip_address: str, exclude_id: int | None = None
) -> None:
    """Пара «магазин + IP» уникальна (§4.11, `uq_store_devices_store_ip`)."""
    stmt = select(StoreDevice.id).where(
        StoreDevice.store_id == store_id, StoreDevice.ip_address == ip_address
    )
    if exclude_id is not None:
        stmt = stmt.where(StoreDevice.id != exclude_id)
    existing = session.scalars(stmt).first()
    if existing is not None:
        raise DeviceValidationError(
            f"Устройство с IP {ip_address} уже заведено в этом магазине (id={existing})"
        )


def bump_devices_version(session: Session, store: Store) -> int:
    """Поднять `stores.devices_version` — сигнал агенту, что список изменился (§7.6)."""
    store.devices_version = int(store.devices_version or 0) + 1
    session.flush()
    logger.info(
        "Магазин id=%s: список оборудования изменён, версия %s",
        store.id,
        store.devices_version,
    )
    return store.devices_version


def create_device(
    session: Session,
    store: Store,
    *,
    name: str,
    ip_address: str,
    device_type: str | None = None,
    check_type: str = CheckType.ICMP.value,
    check_port: int | None = None,
    comment: str | None = None,
) -> StoreDevice:
    """Завести устройство в подсети магазина (§11.7: «Добавить устройство»)."""
    resolved_name = _require_text(name, field="Имя")[:MAX_NAME_LENGTH]
    resolved_ip = _validate_ip(ip_address)
    resolved_type = _validate_type(device_type)
    resolved_check, resolved_port = _validate_check(check_type, check_port)
    _ensure_unique_ip(session, store_id=store.id, ip_address=resolved_ip)

    device = StoreDevice(
        store_id=store.id,
        name=resolved_name,
        ip_address=resolved_ip,
        device_type=resolved_type,
        check_type=resolved_check,
        check_port=resolved_port,
        is_active=True,
        comment=(comment or "").strip()[:MAX_TEXT_LENGTH] or None,
    )
    session.add(device)
    session.flush()
    bump_devices_version(session, store)
    logger.info(
        "Магазин id=%s: устройство %s (%s) добавлено, проверка %s",
        store.id,
        resolved_name,
        resolved_ip,
        resolved_check,
    )
    return device


def update_device(
    session: Session,
    store: Store,
    device: StoreDevice,
    *,
    name: str | None = None,
    ip_address: str | None = None,
    device_type: str | None = None,
    check_type: str | None = None,
    check_port: int | None = None,
    clear_check_port: bool = False,
    is_active: bool | None = None,
    comment: str | None = None,
) -> StoreDevice:
    """Изменить карточку устройства: передаются только изменяемые поля (§5.2.2)."""
    changed = False

    if name is not None:
        device.name = _require_text(name, field="Имя")[:MAX_NAME_LENGTH]
        changed = True
    if ip_address is not None:
        resolved_ip = _validate_ip(ip_address)
        _ensure_unique_ip(
            session, store_id=store.id, ip_address=resolved_ip, exclude_id=device.id
        )
        device.ip_address = resolved_ip
        changed = True
    if device_type is not None:
        device.device_type = _validate_type(device_type)
        changed = True
    if comment is not None:
        device.comment = comment.strip()[:MAX_TEXT_LENGTH] or None
        changed = True

    if check_type is not None or check_port is not None or clear_check_port:
        wanted_type = check_type or str(device.check_type)
        wanted_port = None if clear_check_port else check_port or device.check_port
        device.check_type, device.check_port = _validate_check(wanted_type, wanted_port)
        changed = True

    if is_active is not None:
        device.is_active = bool(is_active)
        changed = True

    if changed:
        session.flush()
        bump_devices_version(session, store)
    return device


def deactivate_device(session: Session, store: Store, device: StoreDevice) -> StoreDevice:
    """Мягкое удаление (§11.7): `is_active=false` — исключение из опроса.

    Строку не удаляем: история статусов и задержек нужна при разборе «камера
    пропала» — устройство просто уходит из push-а и из опроса.
    """
    if device.is_active:
        device.is_active = False
        session.flush()
        bump_devices_version(session, store)
        logger.info("Магазин id=%s: устройство id=%s отключено из опроса", store.id, device.id)
    return device



# --- Push списка и приём результатов (§5.1.2, §5.2.2) -----------------------


def push_payload(session: Session, store: Store) -> DevicesConfigPush:
    """Собрать тело `POST /devices/config` для магазина (§5.1.2, §7.6).

    Уезжают только активные устройства: `is_active=false` — это и есть
    исключение из опроса, о котором агенту знать нечего. Расписание берётся из
    настроек §4.9 (Д-9) — меняется в UI, без правки конфига на каждой кассе.
    """
    interval, timeout, _ = _settings(session)
    devices = list_devices_for_store(session, store.id, include_inactive=False)
    return DevicesConfigPush(
        devices_version=int(store.devices_version or 1),
        devices=[
            DeviceSpec(
                name=item.name,
                ip=item.ip_address,
                check_type=str(item.check_type),
                port=item.check_port,
                is_active=True,
            )
            for item in devices
        ],
        check_interval_sec=interval,
        ping_timeout_ms=timeout,
    )


def apply_status_batch(
    session: Session, store: Store, batch: DeviceStatusBatch
) -> dict[str, int]:
    """Записать результаты опроса от кассы (§5.2.2, §4.11).

    Адреса, которых нет в списке магазина, игнорируются: агент опрашивает
    только явно заведённые устройства, и батч не должен превращаться в способ
    «протащить» чужие адреса (запрет sweep, §7.6).

    `agent_ok=false` означает «касса не смогла опросить»: состояние остаётся
    `unknown`, а не `offline` — не путаем «нет связи с кассой» и «нет связи с
    камерой» (§4.11). Успешный ответ дополнительно пишет `last_seen_at`.
    """
    known = {item.ip_address: item for item in list_devices_for_store(session, store.id)}
    checked_at = utcnow()
    updated = 0
    ignored = 0

    for result in batch.devices:
        device = known.get((result.ip or "").strip())
        if device is None:
            ignored += 1
            continue

        if not batch.agent_ok:
            device.last_status = str(DeviceStatus.UNKNOWN)
            device.last_latency_ms = None
            device.last_error = "касса не смогла опросить подсеть"
            updated += 1
            continue

        device.last_status = str(result.status)
        device.last_error = (result.error or "").strip()[:MAX_TEXT_LENGTH] or None
        if str(result.status) == str(DeviceStatus.ONLINE):
            device.last_seen_at = checked_at
            device.last_latency_ms = result.latency_ms
        else:
            device.last_latency_ms = None
        updated += 1

    session.flush()
    if ignored:
        logger.warning(
            "Магазин id=%s: в батче опроса %s незнакомых адресов — игнорируем (запрет sweep)",
            store.id,
            ignored,
        )
    logger.info(
        "Магазин id=%s: результаты опроса приняты (обновлено %s, agent_ok=%s)",
        store.id,
        updated,
        batch.agent_ok,
    )
    return {"updated": updated, "ignored": ignored}


def count_devices(
    session: Session, *, now: datetime | None = None, window_sec: int | None = None
) -> tuple[int, int]:
    """Счётчики «Устройства онлайн / всего» для плитки главной (§11.1).

    Считаются только устройства активных магазинов, включённые в опрос:
    отключённое устройство — не «офлайн», его просто не проверяют (§11.7).
    """
    reference = now or utcnow()
    window = window_sec if window_sec is not None else _settings(session)[2]
    devices = session.scalars(
        select(StoreDevice)
        .join(Store, Store.id == StoreDevice.store_id)
        .where(Store.is_active.is_(True), StoreDevice.is_active.is_(True))
    ).all()
    online = sum(1 for item in devices if item.is_online(reference, window))
    return online, len(devices)

