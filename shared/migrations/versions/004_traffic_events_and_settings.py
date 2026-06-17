"""add direction/gate to cameras, crossing_events, park_settings; drop daily_stats/daily_person_log

Revision ID: 004_traffic_settings
Revises: 003_daily_person_log
Create Date: 2026-06-17
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '004_traffic_settings'
down_revision: Union[str, Sequence[str], None] = '003_daily_person_log'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Камеры: направление и точка (gate) ──────────────────────────────
    # server_default='in' нужен только чтобы не сломать уже существующие
    # строки; после миграции направление КАЖДОЙ камеры нужно проверить
    # и выставить явно через API (PATCH /cameras/{id}).
    op.add_column(
        'cameras',
        sa.Column('direction', sa.String(3), nullable=False, server_default='in'),
    )
    op.add_column(
        'cameras',
        sa.Column('gate', sa.String(128), nullable=True),
    )
    op.create_check_constraint(
        'ck_camera_direction', 'cameras', "direction in ('in','out')"
    )
    # дальше новые камеры должны указывать direction явно (без дефолта на уровне приложения)
    op.alter_column('cameras', 'direction', server_default=None)

    # ── Журнал пересечений ───────────────────────────────────────────────
    op.create_table(
        'crossing_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('camera_id', sa.Integer(), sa.ForeignKey('cameras.id'), nullable=False),
        sa.Column('direction', sa.String(3), nullable=False),
        sa.Column('gate', sa.String(128), nullable=True),
        sa.Column('person_id', sa.Integer(), sa.ForeignKey('persons.id'), nullable=True),
        sa.Column('age', sa.Integer(), nullable=True),
        sa.Column('gender', sa.Integer(), nullable=True),
        sa.Column('ts', sa.DateTime(), nullable=False),
        sa.CheckConstraint("direction in ('in','out')", name='ck_event_direction'),
    )
    op.create_index('ix_crossing_events_ts', 'crossing_events', ['ts'])
    op.create_index('ix_crossing_events_camera_ts', 'crossing_events', ['camera_id', 'ts'])
    op.create_index('ix_crossing_events_direction_ts', 'crossing_events', ['direction', 'ts'])

    # ── Настройки парка (вместимость / порог / таймзона) ────────────────
    op.create_table(
        'park_settings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('capacity', sa.Integer(), nullable=False, server_default='5000'),
        sa.Column('warning_threshold', sa.Integer(), nullable=False, server_default='4500'),
        sa.Column('timezone', sa.String(64), nullable=False, server_default='UTC'),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )
    op.execute(
        "INSERT INTO park_settings (id, capacity, warning_threshold, timezone) "
        "VALUES (1, 5000, 4500, 'UTC')"
    )

    # ── Старые таблицы дневной дедупликации больше не нужны:
    #    inflow/outflow в ТЗ считаются по каждому событию пересечения,
    #    а не по уникальным посетителям в рамках суток ─────────────────
    op.drop_table('daily_person_log')
    op.drop_table('daily_stats')


def downgrade() -> None:
    op.create_table(
        'daily_stats',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('date', sa.Date(), unique=True),
        sa.Column('total', sa.Integer(), default=0),
        sa.Column('new_faces', sa.Integer(), default=0),
        sa.Column('known_faces', sa.Integer(), default=0),
        sa.Column('male', sa.Integer(), default=0),
        sa.Column('female', sa.Integer(), default=0),
        sa.Column('kids', sa.Integer(), default=0),
        sa.Column('teens', sa.Integer(), default=0),
        sa.Column('adults', sa.Integer(), default=0),
        sa.Column('middle', sa.Integer(), default=0),
        sa.Column('elderly', sa.Integer(), default=0),
        sa.Column('updated_at', sa.DateTime()),
    )
    op.create_table(
        'daily_person_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('person_id', sa.Integer(), nullable=False),
        sa.UniqueConstraint('date', 'person_id', name='uq_daily_person'),
    )

    op.drop_table('park_settings')

    op.drop_index('ix_crossing_events_direction_ts', table_name='crossing_events')
    op.drop_index('ix_crossing_events_camera_ts', table_name='crossing_events')
    op.drop_index('ix_crossing_events_ts', table_name='crossing_events')
    op.drop_table('crossing_events')

    op.drop_constraint('ck_camera_direction', 'cameras', type_='check')
    op.drop_column('cameras', 'gate')
    op.drop_column('cameras', 'direction')
