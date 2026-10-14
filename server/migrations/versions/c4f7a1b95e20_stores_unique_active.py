"""stores: уникальность имени и IP только среди активных магазинов

Revision ID: c4f7a1b95e20
Revises: d9a388454dc8
Create Date: 2026-10-14

«Отключить магазин» (§5.2.2) — мягкое удаление (`is_active=false`), но колонки
`stores.name` и `stores.ip_address` были UNIQUE по всей таблице. Из-за этого
кассу нельзя было завести заново после отключения: сервер отвечал «уже
существует», хотя активной такой записи нет.

Теперь уникальность действует только на активные строки — частичные уникальные
индексы `uq_stores_name_active` и `uq_stores_ip_address_active`
(см. `server/db/models.py::Store.__table_args__` и
`server/services/store_service.py::_ensure_unique`).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4f7a1b95e20'
down_revision: str | None = 'd9a388454dc8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Условие частичного индекса: строка участвует в уникальности, только если активна
ACTIVE_ONLY = sa.text("is_active = 1")


def upgrade() -> None:
    with op.batch_alter_table('stores', schema=None) as batch_op:
        batch_op.drop_index('ix_stores_name')
        batch_op.drop_index('ix_stores_ip_address')
        batch_op.create_index('ix_stores_name', ['name'], unique=False)
        batch_op.create_index('ix_stores_ip_address', ['ip_address'], unique=False)
        batch_op.create_index(
            'uq_stores_name_active', ['name'], unique=True, sqlite_where=ACTIVE_ONLY
        )
        batch_op.create_index(
            'uq_stores_ip_address_active',
            ['ip_address'],
            unique=True,
            sqlite_where=ACTIVE_ONLY,
        )


def downgrade() -> None:
    # Возврат к UNIQUE по всей таблице сработает, только если отключённых
    # магазинов с переиспользованными именем/IP нет: иначе SQLite отклонит
    # создание индекса (это ожидаемо и лечится вручную).
    with op.batch_alter_table('stores', schema=None) as batch_op:
        batch_op.drop_index('uq_stores_ip_address_active')
        batch_op.drop_index('uq_stores_name_active')
        batch_op.drop_index('ix_stores_name')
        batch_op.drop_index('ix_stores_ip_address')
        batch_op.create_index('ix_stores_name', ['name'], unique=True)
        batch_op.create_index('ix_stores_ip_address', ['ip_address'], unique=True)
