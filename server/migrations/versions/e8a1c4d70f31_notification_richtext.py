"""notifications: форматированное тело, «удалять после прочтения», картинки

Revision ID: e8a1c4d70f31
Revises: c4f7a1b95e20
Create Date: 2026-10-14

Э9 «Уведомления 2.0» (§4.6, §11.5):

* `notifications.body_format` — `plain` (прежний простой текст) или `html`
  (разметка из редактора админки). Старые строки остаются `plain`: миграция
  ничего не переписывает, а окно кассы читает формат из этого поля;
* `notifications.delete_after_read` — «удалять после прочтения». По умолчанию
  `false`: прочитанное уведомление остаётся на кассе справочником, `true` —
  исчезает из окна кассы после «Ознакомлен» (в отчёте «X из Y» оно остаётся,
  §4.8);
* таблица `notification_images` — картинки тела. Сам файл лежит на диске
  (`storage/notifications/<id>/<имя>`), в теле — метка `magaz-image:<имя>`
  (`shared/richtext`), поэтому адрес подставляет потребитель: SPA — админский
  API, касса — локальный кэш (§7.4).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e8a1c4d70f31'
down_revision: str | None = 'c4f7a1b95e20'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'body_format',
                sa.String(length=8),
                nullable=False,
                server_default='plain',
            )
        )
        batch_op.add_column(
            sa.Column(
                'delete_after_read',
                sa.Boolean(),
                nullable=False,
                server_default='0',
            )
        )

    op.create_table(
        'notification_images',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('notification_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('content_type', sa.String(length=64), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('checksum', sa.String(length=64), nullable=True),
        sa.Column('created_by', sa.String(length=100), nullable=True),
        sa.ForeignKeyConstraint(
            ['notification_id'], ['notifications.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('notification_id', 'name', name='uq_notification_images_name'),
    )
    with op.batch_alter_table('notification_images', schema=None) as batch_op:
        batch_op.create_index(
            'ix_notification_images_notification_id', ['notification_id'], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table('notification_images', schema=None) as batch_op:
        batch_op.drop_index('ix_notification_images_notification_id')
    op.drop_table('notification_images')

    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.drop_column('delete_after_read')
        batch_op.drop_column('body_format')
