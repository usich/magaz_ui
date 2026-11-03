"""update_cycles: циклы обновления 1С (Д-29)

Revision ID: e5c1a7d92b40
Revises: d1e2f3a4b5c6
Create Date: 2026-11-02

Добавка **Д-29** «Циклы обновления»: вкладка «Обновление 1С» показывала статусы
по последней задаче данного вида за всю историю, поэтому:

* успешное слияние прошлого прохода оставалось зелёным и после старта нового;
* старая ошибка («1С-операция на кассе не выполняется…») светилась красным даже
  после успешного повтора.

Цикл задаёт границу прохода. Новые объекты:

* таблица `update_cycles` — один проход «скачивание → проверка → слияние →
  обновление БД» для магазинов (`scope='stores'`) или локальных баз (`'local'`);
  открывается вручную («Начать новый цикл») либо автоматически, когда открытого
  цикла нет;
* `commands.cycle_id` — к какому циклу относится задача (nullable: история до
  Д-29 и задачи вне цикла — уведомления, сообщения, удаление файлов);
* `commands.batch_id` — «пачка запуска»: один запуск массовой операции на N
  магазинов/баз, чтобы можно было отфильтровать ровно эти задачи и повторять
  только упавшие.

Авто-закрытие по простою выключено по умолчанию (`update_cycle_idle_min=0`):
проход может растягиваться на несколько дней (слияние в пятницу, обновление в
понедельник), и «обрезать» его по времени нельзя.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5c1a7d92b40'
down_revision: str | None = 'd1e2f3a4b5c6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'update_cycles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('scope', sa.String(length=16), nullable=False),
        sa.Column('started_by', sa.String(length=100), nullable=True),
        sa.Column('trigger_kind', sa.String(length=32), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('closed_by', sa.String(length=100), nullable=True),
        sa.Column('closed_reason', sa.String(length=32), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_update_cycles_scope', 'update_cycles', ['scope'])
    op.create_index('ix_update_cycles_closed_at', 'update_cycles', ['closed_at'])
    op.create_index('ix_update_cycles_created_at', 'update_cycles', ['created_at'])

    # SQLite: добавление FK к существующей таблице — только через пересоздание
    # (batch_alter_table), как в Д-24.
    with op.batch_alter_table('commands', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cycle_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('batch_id', sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            'fk_commands_cycle_id_update_cycles',
            'update_cycles',
            ['cycle_id'],
            ['id'],
            ondelete='SET NULL',
        )
        batch_op.create_index('ix_commands_cycle_id', ['cycle_id'])
        batch_op.create_index('ix_commands_batch_id', ['batch_id'])


def downgrade() -> None:
    with op.batch_alter_table('commands', schema=None) as batch_op:
        batch_op.drop_index('ix_commands_batch_id')
        batch_op.drop_index('ix_commands_cycle_id')
        batch_op.drop_constraint('fk_commands_cycle_id_update_cycles', type_='foreignkey')
        batch_op.drop_column('batch_id')
        batch_op.drop_column('cycle_id')

    op.drop_index('ix_update_cycles_created_at', table_name='update_cycles')
    op.drop_index('ix_update_cycles_closed_at', table_name='update_cycles')
    op.drop_index('ix_update_cycles_scope', table_name='update_cycles')
    op.drop_table('update_cycles')
