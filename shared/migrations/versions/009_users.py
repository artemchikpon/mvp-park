"""users: accounts for JWT login (roles admin / viewer)

Revision ID: 009_users
Revises: 008_park_timezone_tashkent
Create Date: 2026-10-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "009_users"
down_revision: Union[str, Sequence[str], None] = "008_park_timezone_tashkent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("role in ('admin','viewer')", name="ck_users_role"),
    )


def downgrade() -> None:
    op.drop_table("users")
