"""stores: колонка extension_name — имя расширения 1С (FR-4)

Revision ID: a1b2c3d4e5f6
Revises: f2b7c9d31a04
Create Date: 2026-10-20

Имя расширения 1С, объединяемого через `/MergeCfg ... -Extension <имя>` (FR-4).
Живёт в карточке магазина (`Store.extension_name`): при массовой операции сервер
подставляет его в `run_merge`, если в параметрах операции имя не задано явно
(`server/core/dispatcher.py::build_1c_payload`).

Добавление сделано batch-режимом (SQLite): хотя `ADD COLUMN` поддерживается,
batch пересоздаёт таблицу аккуратно и не задевает индексы/данные.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: str | None = 'f2b7c9d31a04'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('stores', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('extension_name', sa.String(length=200), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('stores', schema=None) as batch_op:
        batch_op.drop_column('extension_name')
