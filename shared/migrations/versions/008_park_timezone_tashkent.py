"""park_settings.timezone: Asia/Tashkent by default instead of UTC

Revision ID: 008_park_timezone_tashkent
Revises: 007_persons_reset_fk
Create Date: 2026-10-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "008_park_timezone_tashkent"
down_revision: Union[str, Sequence[str], None] = "007_persons_reset_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Парк в Ташкенте. С таймзоной UTC "сегодня" на дашборде начиналось в
    # 05:00 по Ташкенту, почасовые графики были сдвинуты на 5 часов, а
    # ежедневный сброс persons "в 12:00" срабатывал в 17:00.
    # Время событий в БД по-прежнему хранится в UTC — меняется только то,
    # в какой таймзоне считаются сутки и часы.
    op.alter_column(
        "park_settings", "timezone",
        existing_type=sa.String(64), existing_nullable=False,
        server_default="Asia/Tashkent",
    )
    # 'UTC' в существующей строке — значение по умолчанию из миграции 004,
    # а не выбор пользователя; другие значения (заданные через API) не трогаем.
    op.execute("UPDATE park_settings SET timezone = 'Asia/Tashkent' WHERE timezone = 'UTC'")


def downgrade() -> None:
    op.alter_column(
        "park_settings", "timezone",
        existing_type=sa.String(64), existing_nullable=False,
        server_default="UTC",
    )
    op.execute("UPDATE park_settings SET timezone = 'UTC' WHERE timezone = 'Asia/Tashkent'")
