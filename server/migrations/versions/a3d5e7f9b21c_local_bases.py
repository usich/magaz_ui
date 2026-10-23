"""local_bases: локальные базы 1С на машине дашборда

Revision ID: a3d5e7f9b21c
Revises: f2b7c9d31a04
Create Date: 2026-10-22

Д-20 (§Д-19, вкладка «Локальные базы») — сущность «локальная база 1С»:

* реквизиты подключения как у магазина (версия 1С, кластер, имя БД, логин),
  но без агента и IP: база живёт на этой машине, обновление пойдёт напрямую;
* `base_path` — путь к файлу/папке базы для будущего механизма обновления;
* `extension_name` — имя расширения, сливаемого при обновлении;
* `password_enc` — пароль 1С шифруется Fernet (§8.3), наружу уходит только
  признак `password_set`;
* `group` — поле группировки: базы с одинаковым значением админка собирает
  в одну секцию («Бухгалтерия», «Торговля» и т.п.).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3d5e7f9b21c'
down_revision: str | None = 'a1b2c3d4e5f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'local_bases',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('version_1c', sa.String(length=64), nullable=False),
        sa.Column('cluster_name', sa.String(length=200), nullable=False),
        sa.Column('database_name', sa.String(length=200), nullable=False),
        sa.Column('base_path', sa.Text(), nullable=True),
        sa.Column('extension_name', sa.String(length=200), nullable=True),
        sa.Column('user_name_1c', sa.String(length=200), nullable=False),
        sa.Column('password_enc', sa.Text(), nullable=True),
        sa.Column('credentials_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('group', sa.String(length=100), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('local_bases', schema=None) as batch_op:
        batch_op.create_index('ix_local_bases_name', ['name'], unique=False)
        batch_op.create_index('ix_local_bases_group', ['group'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('local_bases', schema=None) as batch_op:
        batch_op.drop_index('ix_local_bases_group')
        batch_op.drop_index('ix_local_bases_name')
    op.drop_table('local_bases')
