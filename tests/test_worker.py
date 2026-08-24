"""
Тесты для services/worker/worker.py.

worker.py импортируется как обычный модуль благодаря рефакторингу: вся
side-effecting инициализация (подключение к БД/Redis, синхронизация
сотрудников, запуск потоков, бесконечный цикл) вынесена в main(), которая
здесь не вызывается — тестируем только чистую логику.

Redis подменяется на fakeredis (in-memory реализация протокола Redis) —
не нужен реальный Redis-сервер и не нужен docker-compose.
"""

from datetime import datetime, timedelta

import fakeredis
import numpy as np
import pytest

import worker as worker_module
from db.models import Camera, Person
from db import stats_service as svc


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """Подменяем redis-клиент воркера на fakeredis для каждого теста —
    чтобы cooldown-ключи из одного теста не утекали в другой."""
    fr = fakeredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr(worker_module, "r", fr)
    return fr


# ── cooldown dedup (_already_counted_recently) ──────────────────────────────

def test_first_sighting_is_not_deduped():
    assert worker_module._already_counted_recently(camera_id=1, person_id=42) is False


def test_second_sighting_within_cooldown_is_deduped():
    worker_module._already_counted_recently(camera_id=1, person_id=42)
    assert worker_module._already_counted_recently(camera_id=1, person_id=42) is True


def test_cooldown_is_scoped_per_camera_not_global():
    """Один и тот же человек на РАЗНЫХ камерах не должен считаться
    дублем — cooldown-ключ включает camera_id."""
    worker_module._already_counted_recently(camera_id=1, person_id=42)
    assert worker_module._already_counted_recently(camera_id=2, person_id=42) is False


def test_cooldown_is_scoped_per_person():
    worker_module._already_counted_recently(camera_id=1, person_id=42)
    assert worker_module._already_counted_recently(camera_id=1, person_id=43) is False


def test_cooldown_key_ttl_matches_configured_window(fake_redis):
    worker_module._already_counted_recently(camera_id=1, person_id=42)
    ttl = fake_redis.ttl(b"crossing_cooldown:1:42")
    assert 0 < ttl <= worker_module.CROSSING_COOLDOWN_SECONDS


# ── camera cache (_reload_cameras / get_camera) ─────────────────────────────

def test_reload_cameras_populates_cache_from_db(db_session):
    db_session.add(Camera(name="A", url="rtsp://a", direction="in", gate="G1"))
    db_session.add(Camera(name="B", url="rtsp://b", direction="out", gate="G1"))
    db_session.commit()

    worker_module._reload_cameras()

    a = worker_module.get_camera(1)
    assert a is not None
    assert a.direction == "in"
    assert a.gate == "G1"


def test_get_camera_returns_none_for_unknown_id(db_session):
    worker_module._reload_cameras()
    assert worker_module.get_camera(9999) is None


def test_reload_cameras_removes_deleted_cameras_from_cache(db_session):
    cam = Camera(name="A", url="rtsp://a", direction="in")
    db_session.add(cam)
    db_session.commit()
    worker_module._reload_cameras()
    assert worker_module.get_camera(cam.id) is not None

    db_session.delete(cam)
    db_session.commit()
    worker_module._reload_cameras()
    assert worker_module.get_camera(cam.id) is None


# ── _process_entry: сквозной путь "кадр из Redis -> событие в БД" ──────────

class _FakeFace:
    def __init__(self, embedding, age=30, gender=1):
        self.embedding = embedding
        self.age = age
        self.gender = gender


def _jpeg_bytes():
    """Минимальный валидный JPEG (1x1 белый пиксель), чтобы cv2.imdecode
    отработал по-настоящему, как и в проде."""
    import cv2
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", frame)
    assert ok
    return buf.tobytes()


def test_process_entry_skips_unknown_camera(monkeypatch, caplog):
    monkeypatch.setattr(worker_module, "get_camera", lambda cid: None)
    calls = []
    monkeypatch.setattr(worker_module, "extract_faces", lambda frame: calls.append(1))

    worker_module._process_entry(b"1-1", {b"camera_id": b"999", b"frame": _jpeg_bytes()})

    assert calls == []  # до детекции лиц дело не дошло


def test_process_entry_skips_frame_without_camera_id():
    # Не должно падать с KeyError — camera_id просто отсутствует в data
    worker_module._process_entry(b"1-1", {b"frame": _jpeg_bytes()})


def test_process_entry_records_crossing_for_new_visitor(db_session, monkeypatch):
    cam = Camera(name="A", url="rtsp://a", direction="in", gate="G1")
    db_session.add(cam)
    db_session.commit()
    worker_module._reload_cameras()

    embedding = np.random.rand(512).astype(np.float32)
    fake_face = _FakeFace(embedding, age=25, gender=1)
    monkeypatch.setattr(worker_module, "extract_faces", lambda frame: [fake_face])
    monkeypatch.setattr(
        worker_module.storage, "identify",
        lambda emb, face: {"id": 1, "new": True, "age": 25, "gender": 1},
    )

    worker_module._process_entry(
        b"1-1", {b"camera_id": str(cam.id).encode(), b"frame": _jpeg_bytes()}
    )

    from db.models import CrossingEvent
    events = db_session.query(CrossingEvent).all()
    assert len(events) == 1
    assert events[0].camera_id == cam.id
    assert events[0].person_id == 1
    assert events[0].direction == "in"


def test_process_entry_skips_faces_identified_as_employee(db_session, monkeypatch):
    """identify() возвращает None для лиц сотрудников — событие не должно
    записываться (см. face_storage_pg.py: 'None означает сотрудник')."""
    cam = Camera(name="A", url="rtsp://a", direction="in")
    db_session.add(cam)
    db_session.commit()
    worker_module._reload_cameras()

    fake_face = _FakeFace(np.random.rand(512).astype(np.float32))
    monkeypatch.setattr(worker_module, "extract_faces", lambda frame: [fake_face])
    monkeypatch.setattr(worker_module.storage, "identify", lambda emb, face: None)

    worker_module._process_entry(
        b"1-1", {b"camera_id": str(cam.id).encode(), b"frame": _jpeg_bytes()}
    )

    from db.models import CrossingEvent
    assert db_session.query(CrossingEvent).count() == 0


def test_process_entry_respects_cooldown_for_repeated_face(db_session, monkeypatch):
    cam = Camera(name="A", url="rtsp://a", direction="in")
    db_session.add(cam)
    db_session.commit()
    worker_module._reload_cameras()

    fake_face = _FakeFace(np.random.rand(512).astype(np.float32))
    monkeypatch.setattr(worker_module, "extract_faces", lambda frame: [fake_face])
    monkeypatch.setattr(
        worker_module.storage, "identify",
        lambda emb, face: {"id": 7, "new": False, "age": 25, "gender": 1},
    )

    entry = {b"camera_id": str(cam.id).encode(), b"frame": _jpeg_bytes()}
    worker_module._process_entry(b"1-1", entry)
    worker_module._process_entry(b"1-2", entry)  # тот же человек, сразу же

    from db.models import CrossingEvent
    assert db_session.query(CrossingEvent).count() == 1  # второй раз — cooldown


# ── ежедневный сброс persons (_seconds_until_next_persons_reset /
#    _run_persons_reset_once) ────────────────────────────────────────────────

def test_seconds_until_next_reset_is_within_24h_and_positive(db_session):
    svc.get_settings(db_session)  # park_settings.timezone по умолчанию UTC
    seconds = worker_module._seconds_until_next_persons_reset()
    assert 0 < seconds <= 24 * 3600


def test_run_persons_reset_once_clears_persons_table(db_session):
    svc.get_settings(db_session)
    db_session.add(Person(embedding=b"\x00" * 8, age=30, gender=1))
    db_session.add(Person(embedding=b"\x01" * 8, age=25, gender=0))
    db_session.commit()
    assert db_session.query(Person).count() == 2

    worker_module._run_persons_reset_once()

    assert db_session.query(Person).count() == 0


def test_run_persons_reset_once_is_a_noop_for_second_replica_same_day(db_session, fake_redis):
    """Вторая реплика воркера, проснувшаяся в ту же минуту, не должна ничего
    удалять повторно — лок в Redis уже занят первой репликой."""
    svc.get_settings(db_session)
    db_session.add(Person(embedding=b"\x00" * 8, age=30, gender=1))
    db_session.commit()

    worker_module._run_persons_reset_once()  # "первая реплика" — реально чистит
    db_session.add(Person(embedding=b"\x02" * 8, age=40, gender=1))
    db_session.commit()

    worker_module._run_persons_reset_once()  # "вторая реплика" — лок уже занят

    # Лицо, добавленное ПОСЛЕ первого сброса, должно остаться нетронутым.
    assert db_session.query(Person).count() == 1


def test_run_persons_reset_once_releases_lock_on_error(db_session, monkeypatch):
    """Если DELETE упал (например, обрыв соединения), лок на сегодня нужно
    снять — иначе таблица останется незачищенной до следующих суток."""
    svc.get_settings(db_session)

    def _boom(session):
        raise RuntimeError("db is down")

    monkeypatch.setattr(worker_module, "reset_persons", _boom)

    worker_module._run_persons_reset_once()

    tz = worker_module._park_timezone()
    today = datetime.now(tz).date().isoformat()
    lock_key = f"{worker_module.PERSONS_RESET_LOCK_KEY}:{today}"
    assert worker_module.r.exists(lock_key) == 0
