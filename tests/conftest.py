

import os
import sys
import types
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# ── 1. sys.path, как в Docker-образах ───────────────────────────────────────
for p in (
    REPO_ROOT / "shared",              # -> `db` package
    REPO_ROOT / "services",            # -> `api` package (services/api/__init__.py)
    REPO_ROOT / "services" / "worker", # -> worker.py, face_engine.py, employee_sync.py как top-level модули
):
    sys.path.insert(0, str(p))

# ── 2. Фейковый face_engine вместо реального insightface ───────────────────
_fake_face_engine = types.ModuleType("face_engine")


def _default_extract_faces(frame):
    """По умолчанию — ни одного лица на кадре. Тесты, которым нужны лица,
    подменяют эту функцию через monkeypatch."""
    return []


_fake_face_engine.extract_faces = _default_extract_faces
sys.modules.setdefault("face_engine", _fake_face_engine)

# ── 3. Тестовая БД: временный sqlite-файл (не in-memory — нужен общий файл
#    между разными Session(), как это будет и в реальном Postgres). ────────
_tmp_db_fd, _tmp_db_path = tempfile.mkstemp(suffix=".sqlite3")
os.close(_tmp_db_fd)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp_db_path}")

# ── 4. API-ключ для services/api/auth.py ────────────────────────────────────
os.environ.setdefault("API_KEYS", "test-key-1,test-key-2")

from db.database import Base, engine, SessionLocal  # noqa: E402
from db import models  # noqa: E402  (регистрирует все таблицы в Base.metadata)


@pytest.fixture()
def db_session():
    """Чистая схема БД на каждый тест: пересоздаём все таблицы."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def api_client(db_session):
    """FastAPI TestClient поверх той же sqlite-схемы, что и db_session
    (роутеры сами открывают SessionLocal() — тот же движок/файл)."""
    from fastapi.testclient import TestClient
    from api.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture()
def api_key_header():
    return {"X-API-Key": "test-key-1"}
