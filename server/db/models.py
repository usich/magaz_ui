"""ORM-модели сервера — таблицы §4 спецификации (SQLAlchemy 2.0, typed style).

Соответствие разделам спецификации:
* `Store`               → §4.1 `stores` (+ `devices_version` из §7.6, Д-1);
* `AdminUser`           → §4.2 `admin_users`;
* `Command`             → §4.3 `commands`;
* `JobLog`              → §4.4 `job_logs`;
* `FileCheck`           → §4.5 `file_checks`;
* `Notification`        → §4.6 `notifications`;
* `NotificationTarget`  → §4.7 `notification_targets`;
* `NotificationReceipt` → §4.8 `notification_receipts`;
* `Setting`             → §4.9 `settings`;
* `AuditLog`            → §4.10 `audit_log`;
* `StoreDevice`         → §4.11 `store_devices`.

Все временные метки — UTC naive (`shared.timeutil.utcnow`).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.db.base import Base
from shared.timeutil import utcnow

__all__ = [
    "AdminUser",
    "AuditLog",
    "Command",
    "FileCheck",
    "JobLog",
    "Notification",
    "NotificationReceipt",
    "NotificationTarget",
    "Setting",
    "Store",
    "StoreDevice",
    "TimestampMixin",
]


class TimestampMixin:
    """Метки создания/изменения (§4: «все таблицы — с `created_at`/`updated_at`»)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )


class Store(TimestampMixin, Base):
    """Магазин = одна касса = один агент (§4.1, Q15)."""

    __tablename__ = "stores"

    # Имя и IP уникальны **только среди активных** магазинов (§4.1 + §5.2.2):
    # «Отключить магазин» — мягкое удаление, и оно освобождает и имя, и IP, чтобы
    # кассу можно было завести заново (переустановка, смена стенда). Правило
    # продублировано в `store_service._ensure_unique` — там текст ошибки для UI.
    __table_args__ = (
        Index(
            "uq_stores_name_active",
            "name",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
        Index(
            "uq_stores_ip_address_active",
            "ip_address",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    ip_address: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent_port: Mapped[int] = mapped_column(Integer, default=8383, server_default="8383", nullable=False)

    # «Имя кластера» из ТЗ = адрес сервера 1С → /S{cluster_name}\{database_name} (Q2)
    cluster_name: Mapped[str] = mapped_column(String(200), nullable=False)
    database_name: Mapped[str] = mapped_column(String(200), nullable=False)
    version_1c: Mapped[str] = mapped_column(String(64), nullable=False)

    # П-1: логин и пароль 1С хранятся на сервере; пароль — только шифрованным (§8.3)
    user_name_1c: Mapped[str] = mapped_column(String(200), nullable=False)
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    credentials_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )

    # Д-1: версия списка оборудования подсети (§7.6). По ней агент понимает,
    # что список нужно перезапросить/push-нуть заново
    devices_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )

    agent_token: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    os_info: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    devices: Mapped[list["StoreDevice"]] = relationship(
        back_populates="store",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="StoreDevice.name",
    )

    @property
    def password_set(self) -> bool:
        """Признак «пароль задан» — наружу отдаём только его, не значение (§8.3)."""
        return bool(self.password_enc)

    def is_online(self, now: datetime | None = None, window_sec: int = 45) -> bool:
        """Онлайн = успешный `GET /health` за последние `window_sec` (§3.3)."""
        if self.last_seen_at is None:
            return False
        reference = now or utcnow()
        return (reference - self.last_seen_at).total_seconds() <= window_sec

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return f"<Store id={self.id} name={self.name!r} ip={self.ip_address!r}>"


class AdminUser(TimestampMixin, Base):
    """Администратор дашборда (§4.2). Сессии — подписанная cookie, таблицы нет."""

    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return f"<AdminUser id={self.id} username={self.username!r}>"


class Command(TimestampMixin, Base):
    """Задача (история операций, §4.3, Q54).

    `params` хранит тело запроса к агенту **без паролей** (I-7): пароль 1С живёт
    только в `stores.password_enc` и уходит на агент отдельным push-ом.
    """

    __tablename__ = "commands"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    log_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_files: Mapped[list | None] = mapped_column(JSON, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    store: Mapped["Store"] = relationship()
    logs: Mapped[list["JobLog"]] = relationship(
        back_populates="command",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="JobLog.id",
    )

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return f"<Command id={self.id} kind={self.kind} status={self.status}>"


class JobLog(Base):
    """Строка журнала задачи (§4.4)."""

    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    command_id: Mapped[int] = mapped_column(
        ForeignKey("commands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    level: Mapped[str] = mapped_column(String(16), default="info", server_default="info", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now(), nullable=False, index=True
    )

    command: Mapped["Command"] = relationship(back_populates="logs")

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return f"<JobLog id={self.id} level={self.level}>"


class FileCheck(Base):
    """Снимок проверки файлов обновления (§4.5, Q52). В UI — последний на магазин."""

    __tablename__ = "file_checks"
    __table_args__ = (
        UniqueConstraint("store_id", "filename", "checked_at", name="uq_file_checks_snapshot"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    cf_exists: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    cfe_exists: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    xml_exists: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    cf_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cfe_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    xml_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cf_mtime: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cfe_mtime: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    xml_mtime: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now(), nullable=False, index=True
    )

    store: Mapped["Store"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return f"<FileCheck store_id={self.store_id} filename={self.filename!r}>"


class StoreDevice(TimestampMixin, Base):
    """Оборудование подсети магазина (Д-1, §4.11).

    Опрашивает **агент** (он физически в подсети), сервер только принимает
    результат батчем. Касса недоступна → `unknown`, а не `offline`.
    """

    __tablename__ = "store_devices"
    __table_args__ = (
        UniqueConstraint("store_id", "ip_address", name="uq_store_devices_store_ip"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    ip_address: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    device_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    check_type: Mapped[str] = mapped_column(
        String(8), default="icmp", server_default="icmp", nullable=False
    )
    check_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    last_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    store: Mapped["Store"] = relationship(back_populates="devices")

    def is_online(self, now: datetime | None = None, window_sec: int = 180) -> bool:
        """Устройство онлайн, если отвечало за последние `window_sec` (§4.11)."""
        if self.last_status != "online" or self.last_seen_at is None:
            return False
        reference = now or utcnow()
        return (reference - self.last_seen_at).total_seconds() <= window_sec

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return f"<StoreDevice id={self.id} ip={self.ip_address!r} status={self.last_status!r}>"


class Notification(TimestampMixin, Base):
    """Уведомление пользователям магазинов (§4.6, ФН3, П-2)."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: Э9: `plain` — прежний простой текст, `html` — разметка из редактора
    #: (`shared.richtext` держит белый список тегов; миграция `e8a1c4d70f31`)
    body_format: Mapped[str] = mapped_column(
        String(8), default="plain", server_default="plain", nullable=False
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    requires_ack: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    repeat_every_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    use_msg_exe: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    #: Э9: «удалять после прочтения». `false` (по умолчанию) — прочитанное
    #: остаётся на кассе справочником, `true` — исчезает после «Ознакомлен»
    delete_after_read: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    targets: Mapped[list["NotificationTarget"]] = relationship(
        back_populates="notification",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="NotificationTarget.id",
    )
    images: Mapped[list["NotificationImage"]] = relationship(
        back_populates="notification",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="NotificationImage.id",
    )

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return f"<Notification id={self.id} severity={self.severity} title={self.title!r}>"


class NotificationTarget(Base):
    """Магазин-получатель уведомления — знаменатель «Y» (§4.7)."""

    __tablename__ = "notification_targets"
    __table_args__ = (
        UniqueConstraint("notification_id", "store_id", name="uq_notification_targets_pair"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    notification_id: Mapped[int] = mapped_column(
        ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sessions_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    notification: Mapped["Notification"] = relationship(back_populates="targets")
    store: Mapped["Store"] = relationship()
    receipts: Mapped[list["NotificationReceipt"]] = relationship(
        back_populates="target",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="NotificationReceipt.id",
    )

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return f"<NotificationTarget id={self.id} notification_id={self.notification_id}>"


class NotificationReceipt(Base):
    """«Ознакомлен»/«отложено» по сессии Windows — числитель «X» (§4.8, П-2)."""

    __tablename__ = "notification_receipts"
    __table_args__ = (
        UniqueConstraint("target_id", "session_id", name="uq_notification_receipts_session"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    target_id: Mapped[int] = mapped_column(
        ForeignKey("notification_targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_user: Mapped[str] = mapped_column(String(100), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", nullable=False, index=True
    )
    shown_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ack_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    snooze_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    target: Mapped["NotificationTarget"] = relationship(back_populates="receipts")

    @property
    def is_acknowledged(self) -> bool:
        """«Ознакомлен» — только явное нажатие; отложенное таковым не считается (П-2)."""
        return self.status == "acknowledged" and self.acknowledged_at is not None

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return f"<NotificationReceipt id={self.id} user={self.session_user!r} status={self.status}>"


class NotificationImage(Base):
    """Картинка в теле уведомления (§4.6, Э9).

    Файл лежит на диске (`storage/notifications/<id уведомления>/<имя>`), в БД —
    только описание. В теле картинка адресуется меткой `magaz-image:<имя>`
    (`shared.richtext`): SPA подставляет адрес админского API, касса — свой
    локальный кэш, поэтому уведомление читается и без интернета (§7.4).
    """

    __tablename__ = "notification_images"
    __table_args__ = (
        UniqueConstraint("notification_id", "name", name="uq_notification_images_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    notification_id: Mapped[int] = mapped_column(
        ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    notification: Mapped["Notification"] = relationship(back_populates="images")

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return (
            f"<NotificationImage id={self.id} notification_id={self.notification_id} "
            f"name={self.name!r} size={self.size_bytes}>"
        )


class Setting(Base):
    """Настройка key/value (§4.9). Значения по умолчанию — в `settings_service`."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )

    def as_int(self) -> int:
        """Прочитать числовую настройку (все ключи §4.9 — целые)."""
        return int(self.value)

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return f"<Setting {self.key}={self.value!r}>"


class AuditLog(Base):
    """Действия администраторов (§4.10). Значения паролей сюда не пишутся никогда."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now(), nullable=False, index=True
    )

    def __repr__(self) -> str:  # pragma: no cover - отладка
        return f"<AuditLog id={self.id} actor={self.actor!r} action={self.action!r}>"





