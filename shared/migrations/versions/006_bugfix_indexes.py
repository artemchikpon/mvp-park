"""bugfix indexes for crossing/person/employee lookups

Revision ID: 006_bugfix_indexes
Revises: 005_add_employees
Create Date: 2026-08-17
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "006_bugfix_indexes"
down_revision: Union[str, Sequence[str], None] = "005_add_employees"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_crossing_events_person_ts",
        "crossing_events",
        ["person_id", "ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_crossing_events_person_ts", table_name="crossing_events")
