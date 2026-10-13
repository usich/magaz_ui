"""Аудит действий администраторов (§4.10).

Важно (I-7, §8.3): в `details` никогда не попадают значения паролей — только
факты («учётные данные 1С изменены»). Хелпер `record` этого не проверяет
автоматически, поэтому вызывающий код обязан маскировать секреты через
`server.core.security.mask_mapping`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.db.models import AuditLog

__all__ = ["list_recent", "record"]


def record(
    session: Session,
    *,
    action: str,
    actor: str | None = None,
    entity: str | None = None,
    entity_id: str | int | None = None,
    details: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    """Записать событие аудита (без commit — транзакцией управляет вызывающий)."""
    entry = AuditLog(
        actor=actor,
        action=action,
        entity=entity,
        entity_id=None if entity_id is None else str(entity_id),
        details=details,
        ip_address=ip_address,
    )
    session.add(entry)
    session.flush()
    return entry


def list_recent(
    session: Session, *, limit: int = 100, offset: int = 0, actor: str | None = None
) -> list[AuditLog]:
    """Последние события аудита (для страницы `/audit`)."""
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit).offset(offset)
    if actor:
        stmt = stmt.where(AuditLog.actor == actor)
    return list(session.scalars(stmt).all())
