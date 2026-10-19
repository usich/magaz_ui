"""notification_categories: категории уведомлений; отказ от дублирующего msg.exe

Revision ID: f2b7c9d31a04
Revises: e8a1c4d70f31
Create Date: 2026-10-15

Спека 1.22 (§4.6, §11.5) — информационная группировка уведомлений:

* таблица `notification_categories` — «Продажи», «Закупки» и прочие категории,
  которые админ заводит в разделе уведомлений. Цвет (`color`, `#RRGGBB`) нужен
  плашке в окне кассы и админке, `sort_order` — порядку в списках, мягкое
  удаление (`is_active=false`) сохраняет историю уже разосланных уведомлений;
* `notifications.category_id` — ссылка на категорию (`NULL` — без категории).
  `ON DELETE SET NULL`: жёсткое удаление категории не задевает уведомления.

Спека 1.22 (§7.4, замена msg.exe тостом): `notifications.use_msg_exe` больше не
нужен — дублирующий канал `msg.exe */localhost` из службы удалён, единственный
канал показа остаётся тостом TrayAgent-а. Колонка снимается, чтобы не осталось
мёртвого поля в контракте (§5.1.2).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2b7c9d31a04'
down_revision: str | None = 'e8a1c4d70f31'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'notification_categories',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('color', sa.String(length=9), nullable=False, server_default='#64748b'),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('created_by', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_notification_categories_name'),
    )
    with op.batch_alter_table('notification_categories', schema=None) as batch_op:
        batch_op.create_index(
            'ix_notification_categories_is_active', ['is_active'], unique=False
        )

    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('category_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_notifications_category_id',
            'notification_categories',
            ['category_id'],
            ['id'],
            ondelete='SET NULL',
        )
        batch_op.create_index('ix_notifications_category_id', ['category_id'], unique=False)
        # Дублирующий канал msg.exe удалён (спека 1.22): поле больше не участвует
        # в контракте, поэтому колонка снимается
        batch_op.drop_column('use_msg_exe')


def downgrade() -> None:
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('use_msg_exe', sa.Boolean(), nullable=False, server_default='1')
        )
        batch_op.drop_index('ix_notifications_category_id')
        batch_op.drop_constraint('fk_notifications_category_id', type_='foreignkey')
        batch_op.drop_column('category_id')

    with op.batch_alter_table('notification_categories', schema=None) as batch_op:
        batch_op.drop_index('ix_notification_categories_is_active')
    op.drop_table('notification_categories')