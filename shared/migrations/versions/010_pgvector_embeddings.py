"""embeddings as pgvector vector(512) instead of raw bytes

Revision ID: 010_pgvector_embeddings
Revises: 009_users
Create Date: 2026-10-15

Векторы лиц (persons.embedding, employees.embedding) хранились как bytea —
сырые байты float32, и искать похожее лицо приходилось в Python у каждого
воркера. Теперь колонки — pgvector `vector(512)`, поиск идёт в Postgres
(оператор <=>). Требует образ Postgres с расширением pgvector
(docker-compose: pgvector/pgvector:0.8.1-pg15-trixie).

Существующие строки конвертируются. persons — дневная галерея: строки с
вектором не той длины (например, из seed-скрипта) удаляются. employees —
постоянные данные: строка не той длины останавливает миграцию с ошибкой.
"""
from typing import Sequence, Union

import numpy as np
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "010_pgvector_embeddings"
down_revision: Union[str, Sequence[str], None] = "009_users"
branch_labels = None
depends_on = None

DIM = 512
TABLES = ("persons", "employees")


def _bytes_to_vector_columns(table: str) -> None:
    conn = op.get_bind()
    op.add_column(table, sa.Column("embedding_vec", Vector(DIM), nullable=True))
    rows = conn.execute(sa.text(f"SELECT id, embedding FROM {table}")).fetchall()
    bad = []
    for row_id, blob in rows:
        vec = np.frombuffer(bytes(blob), dtype=np.float32)
        if vec.size != DIM:
            bad.append(row_id)
            continue
        conn.execute(
            sa.text(f"UPDATE {table} SET embedding_vec = CAST(:v AS vector) WHERE id = :id"),
            {"v": "[" + ",".join(repr(float(x)) for x in vec) + "]", "id": row_id},
        )
    if bad:
        if table == "employees":
            raise RuntimeError(
                f"employees: у строк {bad[:10]} вектор не из {DIM} чисел — "
                f"проверьте данные и пересинхронизируйте сотрудников"
            )
        conn.execute(sa.text(f"DELETE FROM {table} WHERE id = ANY(:ids)"), {"ids": bad})
    op.drop_column(table, "embedding")
    op.alter_column(table, "embedding_vec", new_column_name="embedding", nullable=False)


def _vector_to_bytes_columns(table: str) -> None:
    conn = op.get_bind()
    op.add_column(table, sa.Column("embedding_bytes", sa.LargeBinary(), nullable=True))
    rows = conn.execute(sa.text(f"SELECT id, embedding::text FROM {table}")).fetchall()
    for row_id, vec_text in rows:
        vec = np.array([float(x) for x in vec_text.strip("[]").split(",")], dtype=np.float32)
        conn.execute(
            sa.text(f"UPDATE {table} SET embedding_bytes = :b WHERE id = :id"),
            {"b": vec.tobytes(), "id": row_id},
        )
    op.drop_column(table, "embedding")
    op.alter_column(table, "embedding_bytes", new_column_name="embedding", nullable=False)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    for table in TABLES:
        _bytes_to_vector_columns(table)


def downgrade() -> None:
    for table in TABLES:
        _vector_to_bytes_columns(table)
    # Расширение не удаляем: им могут пользоваться другие объекты базы.
