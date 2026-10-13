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


# ── ограничение частоты кадров (PRODUCER_MAX_FPS) ───────────────────────────

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _passed(limiter, clock, fps_in: float, seconds: float) -> int:
    """Сколько кадров пропустит limiter, если камера даёт fps_in кадров/с."""
    passed = 0
    for _ in range(int(fps_in * seconds)):
        passed += limiter.ready()
        clock.t += 1.0 / fps_in
    return passed


@pytest.mark.parametrize("fps_in", [25, 30, 12.5, 6])
def test_limiter_caps_to_5fps_for_typical_camera_rates(fps_in):
    clock = _Clock()
    limiter = producer.FrameRateLimiter(5, clock=clock)
    passed = _passed(limiter, clock, fps_in, seconds=10)
    assert 49 <= passed <= 51  # 5 fps * 10 s


def test_limiter_does_not_reduce_slower_camera():
    clock = _Clock()
    limiter = producer.FrameRateLimiter(5, clock=clock)
    assert _passed(limiter, clock, fps_in=3, seconds=10) == 30


def test_limiter_zero_means_no_limit():
    clock = _Clock()
    limiter = producer.FrameRateLimiter(0, clock=clock)
    assert _passed(limiter, clock, fps_in=25, seconds=2) == 50


def test_limiter_does_not_burst_after_long_pause():
    """После паузы (переподключение камеры) не должно быть пачки кадров
    "в счёт пропущенного времени"."""
    clock = _Clock()
    limiter = producer.FrameRateLimiter(5, clock=clock)
    _passed(limiter, clock, fps_in=25, seconds=1)
    clock.t += 30  # камера молчала 30 секунд
    assert _passed(limiter, clock, fps_in=25, seconds=1) in (5, 6)


class _FakeCapture:
    """Камера на fps кадров/с: каждый grab() сдвигает часы на 1/fps.
    После max_frames кадров выставляет stop, чтобы цикл завершился."""

    def __init__(self, clock, stop, fps=25, max_frames=250):
        self.clock, self.stop, self.fps, self.left = clock, stop, fps, max_frames
        self.grabs = self.retrieves = 0

    def grab(self):
        self.grabs += 1
        self.clock.t += 1.0 / self.fps
        self.left -= 1
        if self.left <= 0:
            self.stop.set()
        return True

    def retrieve(self):
        import numpy as np
        self.retrieves += 1
        return True, np.zeros((4, 4, 3), dtype=np.uint8)

    def release(self):
        pass


def test_stream_camera_sends_5fps_and_reads_every_frame(monkeypatch):
    import fakeredis
    clock = _Clock()
    stop = threading.Event()
    cap = _FakeCapture(clock, stop, fps=25, max_frames=250)  # 10 секунд видео
    fr = fakeredis.FakeRedis()

    monkeypatch.setattr(producer, "r", fr)
    monkeypatch.setattr(producer, "MAX_FPS", 5.0)
    monkeypatch.setattr(producer.time, "monotonic", clock)
    monkeypatch.setattr(producer.cv2, "VideoCapture", lambda url: cap)
    # FrameRateLimiter берёт clock по умолчанию при определении класса —
    # подменяем фабрику, чтобы он использовал фейковые часы.
    real_limiter = producer.FrameRateLimiter
    monkeypatch.setattr(producer, "FrameRateLimiter", lambda fps: real_limiter(fps, clock=clock))

    producer.stream_camera(7, "rtsp://fake", stop)

    sent = fr.xlen(producer.FRAMES_STREAM)
    assert cap.grabs == 250                 # из потока вычитан каждый кадр
    assert 49 <= sent <= 51                 # в Redis ушло ~5 кадров/с
    assert cap.retrieves == sent            # изображение собиралось только для отправленных
    _, fields = fr.xrange(producer.FRAMES_STREAM, count=1)[0]
    assert fields[b"camera_id"] == b"7"
