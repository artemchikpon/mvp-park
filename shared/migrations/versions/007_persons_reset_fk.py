"""crossing_events.person_id ON DELETE SET NULL (for daily persons reset)

Revision ID: 007_persons_reset_fk
Revises: 006_bugfix_indexes
Create Date: 2026-08-20
"""
from typing import Sequence, Union
from alembic import op


revision: str = "007_persons_reset_fk"
down_revision: Union[str, Sequence[str], None] = "006_bugfix_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Таблица persons теперь очищается раз в сутки (см. worker: сброс в
    # 12:00 по таймзоне парка). Без ON DELETE SET NULL DELETE FROM persons
    # падал бы с нарушением внешнего ключа, пока в crossing_events есть
    # хоть одна ссылающаяся строка.
    op.drop_constraint(
        "crossing_events_person_id_fkey", "crossing_events", type_="foreignkey"
    )
    op.create_foreign_key(
        "crossing_events_person_id_fkey",
        "crossing_events",
        "persons",
        ["person_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "crossing_events_person_id_fkey", "crossing_events", type_="foreignkey"
    )
    op.create_foreign_key(
        "crossing_events_person_id_fkey",
        "crossing_events",
        "persons",
        ["person_id"],
        ["id"],
    )
