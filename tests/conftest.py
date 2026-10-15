import os
import sys
import types
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# ── 1. sys.path, как в Docker-образах ───────────────────────────────────────
for p in (
    REPO_ROOT / "shared",                # -> пакеты `db`, `core`
    REPO_ROOT / "services",              # -> `api` package (services/api/__init__.py)
    REPO_ROOT / "services" / "worker",   # -> модули воркера как top-level (как в образе)
    REPO_ROOT / "services" / "producer", # -> camera.py
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

# ── 4. Секрет JWT для services/api/security.py ──────────────────────────────
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-" + "x" * 32)

from db.database import Base, engine, SessionLocal  # noqa: E402
from db import models  # noqa: E402,F401  (регистрирует все таблицы в Base.metadata)


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


# ── JWT: пользователи и токены ──────────────────────────────────────────────

@pytest.fixture()
def make_user(db_session):
    from api import security
    from db.models import User

    def _make(username="admin", password="admin-pass-123", role="admin", is_active=True):
        user = User(username=username, password_hash=security.hash_password(password),
                    role=role, is_active=is_active)
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        return user

    return _make


@pytest.fixture()
def bearer(make_user):
    """Заголовок Authorization для нового пользователя с ролью role."""
    from api import security

    def _bearer(role="admin", username=None):
        user = make_user(username=username or f"{role}-user", role=role)
        return {"Authorization": f"Bearer {security.create_access_token(user.id)}"}

    return _bearer


@pytest.fixture()
def auth_header(bearer):
    """Authorization для admin — для тестов роутов, где роль не важна."""
    return bearer("admin")
