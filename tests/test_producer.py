"""
Тесты для services/producer/camera.py: управление потоками камер
(sync_workers). Сам захват RTSP (stream_camera) не тестируем — это тонкая
обёртка над cv2.VideoCapture.
"""

import threading

import pytest

import camera as producer


class _FakeThread:
    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self):
        return self.alive


@pytest.fixture()
def started(monkeypatch):
    """Подменяем запуск потока: записываем, какие камеры стартовали."""
    calls = []

    def fake_start(cam_id, url):
        calls.append((cam_id, url))
        return producer.CameraWorker(thread=_FakeThread(), stop=threading.Event(), url=url)

    monkeypatch.setattr(producer, "_start", fake_start)
    return calls


def test_starts_new_cameras(started):
    workers = {}
    producer.sync_workers(workers, {1: "rtsp://a", 2: "rtsp://b"})
    assert sorted(started) == [(1, "rtsp://a"), (2, "rtsp://b")]
    assert set(workers) == {1, 2}


def test_stops_removed_cameras(started):
    workers = {}
    producer.sync_workers(workers, {1: "rtsp://a"})
    old = workers[1]

    producer.sync_workers(workers, {})

    assert workers == {}
    assert old.stop.is_set()


def test_keeps_healthy_camera_untouched(started):
    workers = {}
    producer.sync_workers(workers, {1: "rtsp://a"})
    producer.sync_workers(workers, {1: "rtsp://a"})
    assert started == [(1, "rtsp://a")]


def test_restarts_camera_when_url_changes(started):
    workers = {}
    producer.sync_workers(workers, {1: "rtsp://a"})
    old = workers[1]

    producer.sync_workers(workers, {1: "rtsp://new"})

    assert old.stop.is_set()
    assert workers[1].url == "rtsp://new"
    assert started[-1] == (1, "rtsp://new")


def test_restarts_dead_thread(started):
    workers = {}
    producer.sync_workers(workers, {1: "rtsp://a"})
    workers[1].thread.alive = False

    producer.sync_workers(workers, {1: "rtsp://a"})

    assert len(started) == 2
    assert workers[1].thread.is_alive()


def test_get_active_cameras_returns_only_active(db_session):
    from db.models import Camera
    db_session.add_all([
        Camera(name="A", url="rtsp://a", direction="in", active=True),
        Camera(name="B", url="rtsp://b", direction="out", active=False),
    ])
    db_session.commit()

    assert list(producer.get_active_cameras().values()) == ["rtsp://a"]
