"""
Тесты для services/worker/employee_sync.py без сети: HR API и детектор лиц
подменяются через monkeypatch.
"""

import numpy as np
import pytest

import employee_sync
from db.models import Employee

FRAME = np.zeros((2, 2, 3), dtype=np.uint8)


class _Face:
    def __init__(self, embedding):
        self.embedding = embedding


def _emb(seed):
    return np.random.RandomState(seed).randn(512).astype(np.float32)


@pytest.fixture()
def hr(monkeypatch):
    """Фейковый HR: employees — список сотрудников, photos — file_id -> лицо
    (None = фото не скачалось, [] = лица нет)."""
    state = {"employees": [], "photos": {}}
    monkeypatch.setattr(employee_sync, "_fetch_employees", lambda: state["employees"])

    def fetch_photo(file_id):
        return None if state["photos"].get(file_id) is None else ("frame", file_id)

    def extract(frame):
        return state["photos"][frame[1]]

    monkeypatch.setattr(employee_sync, "_fetch_photo_as_frame", fetch_photo)
    monkeypatch.setattr(employee_sync, "extract_faces", extract)
    return state


def test_sync_counts_added_skipped_and_errors(db_session, hr):
    hr["employees"] = [
        {"id": 1, "firstname": "Анна", "file": 10},   # добавится
        {"id": 2, "firstname": "Борис", "file": 0},   # нет фото -> skipped
        {"id": 3, "firstname": "Вера", "file": 30},   # фото не скачалось -> errors
        {"id": 4, "firstname": "Глеб", "file": 40},   # лица нет -> skipped
        {"id": 5, "firstname": "Дина", "file": 50},   # embedding None -> skipped
    ]
    hr["photos"] = {10: [_Face(_emb(1))], 40: [], 50: [_Face(None)]}

    result = employee_sync.sync_employees()

    assert result == {"added": 1, "updated": 0, "skipped": 3, "errors": 1}
    [emp] = db_session.query(Employee).all()
    assert (emp.external_id, emp.first_name, emp.file_id) == (1, "Анна", 10)
    assert np.allclose(np.asarray(emp.embedding), _emb(1))


def test_sync_updates_existing_employee_by_external_id(db_session, hr):
    db_session.add(Employee(external_id=1, first_name="Old", file_id=1, embedding=np.full(512, 0, np.float32)))
    db_session.commit()
    hr["employees"] = [{"id": 1, "firstname": "Анна", "file": 10}]
    hr["photos"] = {10: [_Face(_emb(1))]}

    assert employee_sync.sync_employees() == {"added": 0, "updated": 1, "skipped": 0, "errors": 0}
    db_session.expire_all()
    [emp] = db_session.query(Employee).all()
    assert (emp.first_name, emp.file_id) == ("Анна", 10)


def test_sync_with_unavailable_hr_returns_zeros(db_session, hr):
    assert employee_sync.sync_employees() == {"added": 0, "updated": 0, "skipped": 0, "errors": 0}


def test_add_employee_creates_then_updates(db_session, hr):
    hr["photos"] = {10: [_Face(_emb(1))], 11: [_Face(_emb(2))]}

    created = employee_sync.add_employee_from_file("Анна", 10, external_id=5)
    assert created["external_id"] == 5 and created["file_id"] == 10
    assert created["created_at"] is not None

    updated = employee_sync.add_employee_from_file("Анна Б.", 11, external_id=5)
    assert updated["id"] == created["id"]
    assert updated["first_name"] == "Анна Б."
    assert db_session.query(Employee).count() == 1


def test_add_employee_without_external_id_always_inserts(db_session, hr):
    hr["photos"] = {10: [_Face(_emb(1))]}
    employee_sync.add_employee_from_file("Анна", 10)
    employee_sync.add_employee_from_file("Анна", 10)
    assert db_session.query(Employee).count() == 2


@pytest.mark.parametrize("photo,exc,message", [
    (None, RuntimeError, "Не удалось скачать или декодировать фото file_id=10"),
    ([], ValueError, "Лицо не найдено на фотографии"),
    ([_Face(None)], ValueError, "Не удалось извлечь embedding лица"),
])
def test_add_employee_errors_keep_messages_api_relies_on(db_session, hr, photo, exc, message):
    hr["photos"] = {10: photo}
    with pytest.raises(exc, match=message):
        employee_sync.add_employee_from_file("Анна", 10)


def test_sync_resolves_insert_race_as_update(db_session, hr, monkeypatch, caplog):
    """Две реплики одновременно не нашли external_id; вторая получает ошибку
    уникальности на INSERT и должна сделать UPDATE (counts: updated)."""
    db_session.add(Employee(external_id=1, first_name="Old", file_id=1, embedding=np.full(512, 0, np.float32)))
    db_session.commit()
    hr["employees"] = [{"id": 1, "firstname": "Анна", "file": 10}]
    hr["photos"] = {10: [_Face(_emb(1))]}

    # Первый поиск сотрудника по external_id "промахивается" (как у реплики,
    # которая искала до COMMIT соседней), дальше — реальные запросы.
    from sqlalchemy.orm import Query
    real_first = Query.first
    calls = {"n": 0}

    def first_misses_once(self):
        if self.column_descriptions[0]["entity"] is Employee:
            calls["n"] += 1
            if calls["n"] == 1:
                return None
        return real_first(self)

    monkeypatch.setattr(Query, "first", first_misses_once)
    caplog.set_level("INFO", logger="employee_sync")

    assert employee_sync.sync_employees() == {"added": 0, "updated": 1, "skipped": 0, "errors": 0}
    assert "Гонка INSERT разрешена UPDATE: Анна (external_id=1)" in caplog.text
    db_session.expire_all()
    [emp] = db_session.query(Employee).all()
    assert emp.first_name == "Анна"
