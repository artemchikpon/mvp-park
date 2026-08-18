"""
Тесты для services/worker/face_storage_pg.py — сопоставление лиц с базой
(cosine similarity), фильтрация сотрудников, скользящее среднее эмбеддинга.
"""

import numpy as np
import pytest

from face_storage_pg import FaceStoragePG, SIMILARITY
from db.models import Person, Employee


@pytest.fixture()
def storage():
    return FaceStoragePG()


def _vec(seed: int, dim: int = 512) -> np.ndarray:
    rng = np.random.RandomState(seed)
    # randn (не rand!) — гауссовский шум со знаком. Для двух независимых
    # случайных векторов такого рода cosine similarity в среднем ~0 и не
    # превышает порог SIMILARITY, в отличие от rand() (все компоненты > 0),
    # где случайные векторы в высокой размерности оказываются обманчиво
    # "похожи" просто из-за общего положительного смещения.
    v = rng.randn(dim).astype(np.float32)
    return v / np.linalg.norm(v)  # нормализуем — реальные эмбеддинги insightface тоже нормированы


class _Face:
    def __init__(self, age=30, gender=1):
        self.age = age
        self.gender = gender


# ── is_employee ──────────────────────────────────────────────────────────────

def test_is_employee_false_when_no_employees_at_all(db_session, storage):
    emb = _vec(1)
    assert storage.is_employee(emb, db_session) is False


def test_is_employee_true_for_matching_embedding(db_session, storage):
    emb = _vec(1)
    db_session.add(Employee(first_name="Иван", embedding=emb.tobytes()))
    db_session.commit()

    # То же самое лицо (небольшой шум, как при повторной съёмке) должно опознаться
    noisy = emb + np.random.RandomState(2).normal(0, 0.01, size=emb.shape).astype(np.float32)
    assert storage.is_employee(noisy, db_session) is True


def test_is_employee_false_for_clearly_different_face(db_session, storage):
    db_session.add(Employee(first_name="Иван", embedding=_vec(1).tobytes()))
    db_session.commit()

    different = _vec(999)
    assert storage.is_employee(different, db_session) is False


# ── identify: новое лицо vs известное ───────────────────────────────────────

def test_identify_creates_new_person_on_first_sighting(db_session, storage):
    emb = _vec(1)
    result = storage.identify(emb, _Face(age=25, gender=1))

    assert result is not None
    assert result["new"] is True
    assert db_session.query(Person).count() == 1


def test_identify_matches_existing_person_by_similarity(db_session, storage):
    emb = _vec(1)
    person = Person(embedding=emb.tobytes(), age=25, gender=1)
    db_session.add(person)
    db_session.commit()

    noisy = emb + np.random.RandomState(3).normal(0, 0.01, size=emb.shape).astype(np.float32)
    result = storage.identify(noisy, _Face(age=25, gender=1))

    assert result is not None
    assert result["id"] == person.id
    # Не должно быть создано второй записи для того же человека
    assert db_session.query(Person).count() == 1


def test_identify_creates_separate_person_for_different_face(db_session, storage):
    db_session.add(Person(embedding=_vec(1).tobytes(), age=25, gender=1))
    db_session.commit()

    result = storage.identify(_vec(999), _Face(age=40, gender=0))

    assert result is not None
    assert db_session.query(Person).count() == 2


def test_identify_returns_none_for_employee_face(db_session, storage):
    emb = _vec(1)
    db_session.add(Employee(first_name="Иван", embedding=emb.tobytes()))
    db_session.commit()

    result = storage.identify(emb, _Face(age=30, gender=1))

    assert result is None
    # Сотрудник не должен попасть в таблицу Person
    assert db_session.query(Person).count() == 0


def test_identify_prefers_best_matching_person_when_multiple_close(db_session, storage):
    """Если найдено несколько похожих Person, должен выбираться тот, у кого
    similarity выше, а не первый попавшийся."""
    base = _vec(1)
    close_match = base + np.random.RandomState(10).normal(0, 0.001, size=base.shape).astype(np.float32)
    far_match = base + np.random.RandomState(20).normal(0, 0.2, size=base.shape).astype(np.float32)

    p_close = Person(embedding=close_match.tobytes(), age=20, gender=1)
    p_far = Person(embedding=far_match.tobytes(), age=20, gender=1)
    db_session.add_all([p_far, p_close])  # добавлены в порядке "далёкий раньше близкого"
    db_session.commit()

    result = storage.identify(base, _Face(age=20, gender=1))
    assert result["id"] == p_close.id
