"""local_base_update: задачи обновления локальных баз 1С (Д-24)

Revision ID: d1e2f3a4b5c6
Revises: b6e4f8a2c3d0
Create Date: 2026-10-23

Добавка **Д-24** «Обновление локальных баз 1С»: обновление локальной базы
выполняется напрямую на машине дашборда (`1cv8.exe`), без агента. Чтобы
задача/журнал попали в ту же историю `/jobs`, что и операции магазинов,
используется таблица `commands`, а не отдельная сущность:

* `commands.store_id` становится **nullable** — у локальной задачи его нет;
* добавляется `commands.local_base_id` — nullable FK на `local_bases`
  (`ondelete=CASCADE`): локальная команда ссылается на базу, магазинная — нет.

Отключение магазина не трогает `store_id` (он остаётся как есть), поэтому
`ondelete=CASCADE` только на новом FK и не влияет на существующие задачи.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1e2f3a4b5c6'
down_revision: str | None = 'b6e4f8a2c3d0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite не умеет менять NOT NULL на колонке без пересоздания таблицы —
    # batch_alter_table делает это через пересоздание таблицы (как в Д-21).
    with op.batch_alter_table('commands', schema=None) as batch_op:
        batch_op.alter_column('store_id', existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(
            sa.Column('local_base_id', sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            'fk_commands_local_base_id_local_bases',
            'local_bases',
            ['local_base_id'],
            ['id'],
            ondelete='CASCADE',
        )
        batch_op.create_index('ix_commands_local_base_id', ['local_base_id'])


def downgrade() -> None:
    with op.batch_alter_table('commands', schema=None) as batch_op:
        batch_op.drop_index('ix_commands_local_base_id')
        batch_op.drop_constraint('fk_commands_local_base_id_local_bases', type_='foreignkey')
        batch_op.drop_column('local_base_id')
        # Вернуть NOT NULL нельзя безопасно для строк без store_id; оставляем
        # nullable — даунгрейд не восстанавливает данные, только схему.
        batch_op.alter_column('store_id', existing_type=sa.Integer(), nullable=True)
