"""
Разделение GPU / CPU: выбор устройства (device.py) и роль overflow-воркера
(stream_consumer), который берёт кадры только когда очередь копится.
"""

import fakeredis
import pytest

import device
import redis_conn
import stream_consumer
from core.redis_keys import FRAMES_CONSUMER_GROUP, FRAMES_STREAM


# ── device.py ────────────────────────────────────────────────────────────────

def test_cpu_device_uses_only_cpu_provider():
    assert device.providers_for("cpu") == [device.CPU]


def test_gpu_device_prefers_cuda_with_cpu_fallback():
    assert device.providers_for(" GPU ") == [device.CUDA, device.CPU]


def test_unknown_device_is_rejected():
    with pytest.raises(ValueError):
        device.providers_for("tpu")


@pytest.mark.parametrize("sessions,expected", [
    ([[device.CUDA, device.CPU], [device.CUDA, device.CPU]], "gpu"),
    ([[device.CPU], [device.CPU]], "cpu"),   # GPU запрошен, но onnxruntime откатился на CPU
    ([[device.CUDA, device.CPU], [device.CPU]], "gpu"),
    ([], "cpu"),
])
def test_active_device_is_detected_from_sessions(sessions, expected):
    assert device.active_device(sessions) == expected


class _FakeSessionOptions:
    def __init__(self):
        self.config = {}
        self.intra_op_num_threads = 0

    def add_session_config_entry(self, key, value):
        self.config[key] = value


@pytest.fixture()
def fake_ort(monkeypatch):
    # onnxruntime в тестовом окружении не ставится (см. requirements-dev.txt)
    import sys
    import types
    monkeypatch.setitem(sys.modules, "onnxruntime",
                        types.SimpleNamespace(SessionOptions=_FakeSessionOptions))


def test_session_options_disable_spinning_and_keep_auto_threads(fake_ort):
    opts = device.session_options()
    assert opts.config == {
        "session.intra_op.allow_spinning": "0",
        "session.inter_op.allow_spinning": "0",
    }
    assert opts.intra_op_num_threads == 0  # 0 — число потоков выбирает onnxruntime


def test_session_options_limit_threads(fake_ort):
    assert device.session_options(2).intra_op_num_threads == 2


def test_preload_cuda_libs_never_raises():
    device.preload_cuda_libs()  # на машине без GPU/onnxruntime-gpu — просто ничего


# ── overflow-роль ────────────────────────────────────────────────────────────

@pytest.fixture()
def stream(monkeypatch):
    fr = fakeredis.FakeRedis()
    monkeypatch.setattr(redis_conn, "r", fr)
    fr.xgroup_create(FRAMES_STREAM, FRAMES_CONSUMER_GROUP, id="$", mkstream=True)
    return fr


def _add_frames(r, n):
    for _ in range(n):
        r.xadd(FRAMES_STREAM, {"camera_id": "1", "frame": b"x"})


def test_undelivered_count_tracks_backlog(stream):
    assert stream_consumer.undelivered_count(10) == 0
    _add_frames(stream, 7)
    assert stream_consumer.undelivered_count(10) == 7
    stream.xreadgroup(FRAMES_CONSUMER_GROUP, "gpu", {FRAMES_STREAM: ">"}, count=5)
    assert stream_consumer.undelivered_count(10) == 2
    _add_frames(stream, 20)
    assert stream_consumer.undelivered_count(10) == 10  # не считаем дальше limit


def test_undelivered_count_falls_back_to_xrange_when_lag_unknown(stream, monkeypatch):
    _add_frames(stream, 4)
    real = stream.xinfo_groups

    def no_lag(name):
        groups = real(name)
        for g in groups:
            g["lag"] = None
        return groups

    monkeypatch.setattr(stream, "xinfo_groups", no_lag)
    assert stream_consumer.undelivered_count(10) == 4


def test_primary_worker_always_reads(stream, monkeypatch):
    monkeypatch.setattr(stream_consumer, "WORKER_ROLE", "primary")
    assert stream_consumer._should_read_new() is True


def test_overflow_worker_waits_while_gpu_keeps_up(stream, monkeypatch):
    monkeypatch.setattr(stream_consumer, "WORKER_ROLE", "overflow")
    monkeypatch.setattr(stream_consumer, "OVERFLOW_MIN_BACKLOG", 10)
    _add_frames(stream, 3)  # GPU-воркер справляется: очередь маленькая
    assert stream_consumer._should_read_new() is False


def test_overflow_worker_joins_when_backlog_grows(stream, monkeypatch):
    monkeypatch.setattr(stream_consumer, "WORKER_ROLE", "overflow")
    monkeypatch.setattr(stream_consumer, "OVERFLOW_MIN_BACKLOG", 10)
    _add_frames(stream, 12)  # GPU не успевает или упал
    assert stream_consumer._should_read_new() is True


def test_overflow_worker_processes_frames_end_to_end(stream, monkeypatch):
    """Сквозной сценарий: пока очередь мала — overflow ничего не берёт;
    когда выросла — берёт кадр, обрабатывает и подтверждает (XACK)."""
    monkeypatch.setattr(stream_consumer, "WORKER_ROLE", "overflow")
    monkeypatch.setattr(stream_consumer, "OVERFLOW_MIN_BACKLOG", 5)
    handled = []
    handler = lambda mid, data: handled.append(mid)

    _add_frames(stream, 2)
    if stream_consumer._should_read_new():
        stream_consumer._read_new(handler)
    assert handled == []

    _add_frames(stream, 5)
    assert stream_consumer._should_read_new()
    stream_consumer._read_new(handler)
    assert len(handled) == 1
    assert stream.xpending(FRAMES_STREAM, FRAMES_CONSUMER_GROUP)["pending"] == 0
