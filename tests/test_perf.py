"""
Тесты замеров производительности: shared/core/perf.py и то, что worker и
producer действительно пишут в них нужные шаги.
"""

import threading

import numpy as np
import pytest

from core import perf


class _Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture()
def clocks():
    return _Clock(100.0), _Clock(5.0)  # wall, cpu


def _stats(clocks):
    wall, cpu = clocks
    return perf.PerfStats("test", clock=wall, cpu_clock=cpu)


def test_report_shows_rates_cpu_and_stage_shares(clocks):
    wall, cpu = clocks
    s = _stats(clocks)
    for _ in range(10):
        s.add_time("model", 0.060)
        s.add_time("match", 0.020)
    s.count("frames", 10)
    s.gauge("gallery_persons", 42)
    s.observe("queue_wait", 0.5)
    wall.t += 10      # прошло 10 секунд
    cpu.t += 15       # процесс потратил 15 с CPU -> 1.5 ядра

    line = s.report()

    assert line.startswith("[perf test] 10.0s")
    assert "CPU 1.50 cores" in line
    assert "frames 10 (1.0/s)" in line
    assert "model 60.0ms avg, max 60ms, x10 (75%)" in line
    assert "match 20.0ms avg, max 20ms, x10 (25%)" in line
    assert line.index("model") < line.index("match")  # самый тяжёлый шаг — первым
    assert "queue_wait 500ms avg, max 500ms" in line
    assert "gallery_persons 42" in line


def test_report_resets_counters(clocks):
    wall, _ = clocks
    s = _stats(clocks)
    s.count("frames", 5)
    s.add_time("model", 0.1)
    wall.t += 1
    s.report()
    wall.t += 1
    line = s.report()
    assert "frames" not in line and "model" not in line


def test_measure_records_time_even_on_exception(clocks):
    wall, _ = clocks
    s = _stats(clocks)
    with pytest.raises(RuntimeError):
        with s.measure("db"):
            wall.t += 0.25
            raise RuntimeError("boom")
    wall.t += 1
    assert "db 250.0ms avg" in s.report()


def test_get_returns_same_instance_per_name():
    assert perf.get("x-same") is perf.get("x-same")
    assert perf.get("x-same") is not perf.get("x-other")


def test_reporter_disabled_with_zero_interval(monkeypatch):
    started = []
    monkeypatch.setattr(perf.threading, "Thread", lambda *a, **kw: started.append(1))
    perf.PerfStats("t").start_reporter(interval_s=0)
    assert started == []


# ── worker: process_frame пишет шаги и счётчики ─────────────────────────────

def test_worker_process_frame_records_stages(db_session, monkeypatch):
    import cv2
    from pipeline import camera_cache
    from pipeline import crossings
    from db.models import Camera

    cam = Camera(name="A", url="rtsp://a", direction="in")
    db_session.add(cam)
    db_session.commit()
    camera_cache.reload_cameras()

    class _Face:
        embedding = np.ones(512, dtype=np.float32)
        age, gender = 30, 1

    monkeypatch.setattr(crossings, "extract_faces", lambda frame: [_Face()])
    monkeypatch.setattr(crossings, "stats", perf.PerfStats("w"))
    import fakeredis
    import redis_conn
    monkeypatch.setattr(redis_conn, "r", fakeredis.FakeRedis())

    ok, buf = cv2.imencode(".jpg", np.zeros((4, 4, 3), np.uint8))
    crossings.process_frame(b"1700000000000-0", {b"camera_id": str(cam.id).encode(), b"frame": buf.tobytes()})

    line = crossings.stats.report()
    for part in ("frames 1", "faces 1", "crossings 1", "new_persons 1",
                 "decode", "model", "redis_cooldown", "db_event", "frame_total", "queue_wait"):
        assert part in line, part


def test_queue_wait_parsed_from_stream_id(monkeypatch):
    from pipeline import crossings
    monkeypatch.setattr(crossings.time, "time", lambda: 1700000002.5)
    assert crossings._queue_wait_s(b"1700000000000-3") == pytest.approx(2.5)
    assert crossings._queue_wait_s(b"garbage") is None


# ── producer: счётчики по камере ─────────────────────────────────────────────

def test_producer_counts_grabbed_and_sent_per_camera(monkeypatch):
    import fakeredis
    import camera as producer

    clock = _Clock(1000.0)
    stop = threading.Event()

    class _Cap:
        left = 50

        def grab(self):
            clock.t += 0.04  # 25 fps
            self.left -= 1
            if self.left <= 0:
                stop.set()
            return True

        def retrieve(self):
            return True, np.zeros((4, 4, 3), np.uint8)

        def release(self):
            pass

    real_limiter = producer.FrameRateLimiter
    monkeypatch.setattr(producer, "r", fakeredis.FakeRedis())
    monkeypatch.setattr(producer, "MAX_FPS", 5.0)
    monkeypatch.setattr(producer.cv2, "VideoCapture", lambda url: _Cap())
    monkeypatch.setattr(producer, "FrameRateLimiter", lambda fps: real_limiter(fps, clock=clock))
    monkeypatch.setattr(producer, "stats", perf.PerfStats("p"))

    producer.stream_camera(3, "rtsp://x", stop)

    line = producer.stats.report()
    assert "cam3.grabbed 50" in line
    assert "cam3.sent 10" in line or "cam3.sent 11" in line  # 2 с при 5 fps
    for part in ("retrieve", "jpeg", "xadd"):
        assert part in line
