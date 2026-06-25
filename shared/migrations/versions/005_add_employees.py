"""add employees table for ignored faces

Revision ID: 005_add_employees
Revises: 004_traffic_settings
Create Date: 2026-06-25
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '005_add_employees'
down_revision: Union[str, Sequence[str], None] = '004_traffic_settings'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Таблица employees — вектора лиц сотрудников.
    При идентификации воркер пропускает (игнорирует) эти лица,
    чтобы сотрудники не попадали в статистику посетителей.

    external_id  — id сотрудника в HR-системе (api/v1/employees)
    first_name   — имя (для отображения в интерфейсе)
    file_id      — id файла/фото в HR-системе
    embedding    — float32 numpy-вектор (512-dim = 2048 bytes)
    created_at   — когда добавлен
    """
    op.create_table(
        'employees',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('external_id', sa.Integer(), nullable=True, unique=True),
        sa.Column('first_name', sa.String(128), nullable=False),
        sa.Column('file_id', sa.Integer(), nullable=True),
        sa.Column('embedding', sa.LargeBinary(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_employees_external_id', 'employees', ['external_id'])


def downgrade() -> None:
    op.drop_index('ix_employees_external_id', table_name='employees')
    op.drop_table('employees')
