"""add cameras and persons tables

Revision ID: 002_cameras_persons
Revises: cf0719dc1cb7
Create Date: 2026-06-11
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '002_cameras_persons'
down_revision: Union[str, Sequence[str], None] = 'cf0719dc1cb7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cameras',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(128), nullable=False),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('active', sa.Boolean(), default=True, nullable=False,
                  server_default='true'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'persons',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('embedding', sa.LargeBinary(), nullable=False),
        sa.Column('age', sa.Integer(), nullable=True),
        sa.Column('gender', sa.Integer(), nullable=True),
        sa.Column('first_seen', sa.DateTime(), nullable=True),
        sa.Column('last_seen', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('persons')
    op.drop_table('cameras')
