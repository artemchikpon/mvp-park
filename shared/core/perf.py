"""Лёгкие замеры производительности: сколько времени занимают шаги обработки
и сколько CPU тратит процесс.

Использование:

    stats = perf.get("worker")
    with stats.measure("model"):
        faces = extract_faces(frame)
    stats.count("frames")
    stats.gauge("gallery_persons", 1234)

Раз в PERF_LOG_INTERVAL секунд (по умолчанию 30, 0 — выключено) фоновый
поток пишет в лог сводку за прошедший интервал и обнуляет счётчики:

    [perf worker] 30.0s | CPU 1.85 cores | frames 148 (4.9/s), faces 170 (5.7/s)
      | model 62.1ms avg, 9190ms total (91%) | match 1.2ms avg ...

Доля (%) — от суммарного времени всех шагов (measure/add_time), т.е.
показывает, что именно съедает время; шаги не должны вкладываться друг в
друга. observe() — для длительностей вне долей (время кадра целиком,
ожидание в очереди). "CPU N cores" — процессорное время всего процесса (все
потоки) за интервал, делённое на длительность интервала.
"""

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from core.env import env_float

log = logging.getLogger("perf")

INTERVAL_S = env_float("PERF_LOG_INTERVAL", 30.0)


@dataclass
class _Timing:
    count: int = 0
    total: float = 0.0
    max: float = 0.0

    def add(self, seconds: float) -> None:
        self.count += 1
        self.total += seconds
        self.max = max(self.max, seconds)


class PerfStats:
    def __init__(self, name: str, clock=time.monotonic, cpu_clock=time.process_time):
        self.name = name
        self._clock = clock
        self._cpu_clock = cpu_clock
        self._lock = threading.Lock()
        self._reset(clock(), cpu_clock())

    def _reset(self, now: float, cpu_now: float) -> None:
        self._timings: dict[str, _Timing] = {}
        self._observed: dict[str, _Timing] = {}
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._started = now
        self._cpu_started = cpu_now

    # ── сбор ─────────────────────────────────────────────────────────────────

    def add_time(self, stage: str, seconds: float) -> None:
        with self._lock:
            self._timings.setdefault(stage, _Timing()).add(seconds)

    @contextmanager
    def measure(self, stage: str):
        start = self._clock()
        try:
            yield
        finally:
            self.add_time(stage, self._clock() - start)

    def observe(self, key: str, seconds: float) -> None:
        """Длительность, которая НЕ входит в доли шагов (например, полное
        время кадра или ожидание в очереди) — в отчёте только avg/max."""
        with self._lock:
            self._observed.setdefault(key, _Timing()).add(seconds)

    def count(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + n

    def gauge(self, key: str, value: float) -> None:
        """Последнее значение (размер галереи, отставание очереди и т.п.)."""
        with self._lock:
            self._gauges[key] = value

    # ── отчёт ────────────────────────────────────────────────────────────────

    def report(self) -> str:
        """Сводка за интервал с прошлого отчёта; счётчики обнуляются."""
        now, cpu_now = self._clock(), self._cpu_clock()
        with self._lock:
            elapsed = max(now - self._started, 1e-9)
            cpu_cores = (cpu_now - self._cpu_started) / elapsed
            timings, observed = self._timings, self._observed
            counters, gauges = self._counters, self._gauges
            self._reset(now, cpu_now)

        parts = [f"[perf {self.name}] {elapsed:.1f}s", f"CPU {cpu_cores:.2f} cores"]
        if counters:
            parts.append(", ".join(
                f"{k} {v} ({v / elapsed:.1f}/s)" for k, v in sorted(counters.items())
            ))
        busy = sum(t.total for t in timings.values()) or 1e-9
        for stage, t in sorted(timings.items(), key=lambda kv: -kv[1].total):
            parts.append(
                f"{stage} {t.total / t.count * 1000:.1f}ms avg, max {t.max * 1000:.0f}ms, "
                f"x{t.count} ({t.total / busy * 100:.0f}%)"
            )
        for key, t in sorted(observed.items()):
            parts.append(f"{key} {t.total / t.count * 1000:.0f}ms avg, max {t.max * 1000:.0f}ms")
        if gauges:
            parts.append(", ".join(f"{k} {v:g}" for k, v in sorted(gauges.items())))
        return " | ".join(parts)

    def start_reporter(self, interval_s: float = INTERVAL_S) -> None:
        """Фоновый поток, раз в interval_s пишет report() в лог. 0 — выключено."""
        if interval_s <= 0:
            return

        def loop():
            while True:
                time.sleep(interval_s)
                try:
                    log.info(self.report())
                except Exception:
                    log.exception("perf report failed")

        threading.Thread(target=loop, name=f"perf-{self.name}", daemon=True).start()


_registry: dict[str, PerfStats] = {}
_registry_lock = threading.Lock()


def get(name: str) -> PerfStats:
    """Один PerfStats на имя в пределах процесса."""
    with _registry_lock:
        if name not in _registry:
            _registry[name] = PerfStats(name)
        return _registry[name]
