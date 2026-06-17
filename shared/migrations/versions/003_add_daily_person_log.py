"""add daily_person_log table

Revision ID: 003_daily_person_log
Revises: 002_cameras_persons
Create Date: 2026-06-11
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '003_daily_person_log'
down_revision: Union[str, Sequence[str], None] = '002_cameras_persons'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'daily_person_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('person_id', sa.Integer(), nullable=False),
        sa.UniqueConstraint('date', 'person_id', name='uq_daily_person'),
    )


def downgrade() -> None:
    op.drop_table('daily_person_log')
