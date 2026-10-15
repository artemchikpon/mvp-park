"""Каждый образ, который копирует shared/db (db/models.py), должен ставить
всё, что импортируют модели. Иначе сервис падает при старте с
ModuleNotFoundError — так было с producer и pgvector."""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MODEL_DEPS = ("SQLAlchemy", "psycopg2-binary", "pgvector")


def _images_with_shared_db():
    for dockerfile in sorted(REPO.glob("**/Dockerfile*")):
        if ".venv" in dockerfile.parts:
            continue
        text = dockerfile.read_text()
        if "COPY shared/db" in text:
            yield dockerfile


@pytest.mark.parametrize("dockerfile", list(_images_with_shared_db()), ids=lambda p: str(p.relative_to(REPO)))
def test_images_with_models_install_model_dependencies(dockerfile):
    requirements = (dockerfile.parent / "requirements.txt").read_text()
    names = {line.split("#")[0].strip().split(">")[0].split("<")[0].split("=")[0].split("[")[0]
             for line in requirements.splitlines()}
    missing = [dep for dep in MODEL_DEPS if dep not in names]
    assert not missing, f"{dockerfile.relative_to(REPO)}: в requirements.txt нет {missing}"
