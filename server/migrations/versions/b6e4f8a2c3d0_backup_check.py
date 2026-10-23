"""backup_check: колонки проверки наличия бэкапов 1С (Д-21)

Revision ID: b6e4f8a2c3d0
Revises: a3d5e7f9b21c
Create Date: 2026-10-22

Добавка **Д-21** «Бэкапы»: на магазины (`stores`) и локальные базы
(`local_bases`) вешаются настройки проверки бэкапа и текущий статус.

* настройки: `backup_check_enabled`, `backup_path` (локальный `C:\\...` или сетевой
  `\\\\server\\share\\...`), `backup_host` (имя машины SMB), `backup_use_auth`
  (не «Гость» → нужны логин/пароль), `backup_user`, `backup_password_enc`
  (Fernet, наружу только признак), `backup_check_yesterday` (за вчера/сегодня);
* текущий статус (история не хранится): `backup_status` (`ok`/`missing`/`''`),
  `backup_error`, `backup_checked_at`, `backup_checked_date`, `backup_tries`
  (сколько попыток за день — для «5 попыток с интервалом 3 мин»).

Batch-режимом: SQLite, `ADD COLUMN` через пересоздание таблиц.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b6e4f8a2c3d0'
down_revision: str | None = 'a3d5e7f9b21c'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Общий набор колонок проверки бэкапа для обеих таблиц
BACKUP_COLUMNS = (
    sa.Column('backup_check_enabled', sa.Boolean(), nullable=False, server_default='0'),
    sa.Column('backup_path', sa.Text(), nullable=True),
    sa.Column('backup_host', sa.String(length=100), nullable=True),
    sa.Column('backup_use_auth', sa.Boolean(), nullable=False, server_default='0'),
    sa.Column('backup_user', sa.String(length=200), nullable=True),
    sa.Column('backup_password_enc', sa.Text(), nullable=True),
    sa.Column('backup_check_yesterday', sa.Boolean(), nullable=False, server_default='0'),
    sa.Column('backup_status', sa.String(length=16), nullable=False, server_default=''),
    sa.Column('backup_error', sa.Text(), nullable=True),
    sa.Column('backup_checked_at', sa.DateTime(), nullable=True),
    sa.Column('backup_checked_date', sa.String(length=10), nullable=True),
    sa.Column('backup_tries', sa.Integer(), nullable=False, server_default='0'),
)


def _add_columns(table: str) -> None:
    with op.batch_alter_table(table, schema=None) as batch_op:
        for column in BACKUP_COLUMNS:
            batch_op.add_column(column)


def _drop_columns(table: str) -> None:
    with op.batch_alter_table(table, schema=None) as batch_op:
        for column in BACKUP_COLUMNS:
            batch_op.drop_column(column.name)


def upgrade() -> None:
    _add_columns('stores')
    _add_columns('local_bases')


def downgrade() -> None:
    _drop_columns('local_bases')
    _drop_columns('stores')
