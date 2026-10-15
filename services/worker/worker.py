"""Точка входа worker-сервиса.

Пакеты:
  recognition/  — устройство (CPU/GPU), модель InsightFace, поиск по галерее
  pipeline/     — чтение кадров из Redis Stream, кадр -> лица -> событие,
                  кэш настроек камер
  employees/    — синхронизация сотрудников с HR API, задачи от api
  maintenance/  — ежедневная очистка галереи persons
"""

import logging
import threading

# До импорта модулей воркера: crossings импортирует face_engine, а тот
# загружает модель и пишет в лог устройство ("InsightFace device: ...")
# прямо при импорте. Без настройки логирования эта INFO-строка терялась.
logging.basicConfig(level=logging.INFO)

from pipeline import camera_cache  # noqa: E402
from pipeline import crossings  # noqa: E402
from employees import jobs as employee_jobs  # noqa: E402
from employees import sync as employee_sync  # noqa: E402
from maintenance import persons_reset  # noqa: E402
from pipeline import stream_consumer  # noqa: E402
import config as worker_config  # noqa: E402
from core import perf  # noqa: E402
from core.redis_utils import wait_for_redis  # noqa: E402
from db.database import wait_for_db  # noqa: E402

log = logging.getLogger("worker")


def _start_daemon(target, name: str) -> None:
    threading.Thread(target=target, name=name, daemon=True).start()


def main() -> None:
    wait_for_db()
    wait_for_redis()
    stream_consumer.ensure_consumer_group()

    # face_engine загружает модель при импорте и определяет фактическое
    # устройство; подписываем им отчёты [perf ...], чтобы GPU- и CPU-воркеры
    # было видно отдельно.
    from recognition.face_engine import ACTIVE_DEVICE

    stats = perf.get("worker")
    stats.name = f"worker {ACTIVE_DEVICE} {worker_config.WORKER_ROLE}"
    log.info(f"Worker started: device={ACTIVE_DEVICE}, role={worker_config.WORKER_ROLE}"
             + (f", overflow from backlog >= {worker_config.OVERFLOW_MIN_BACKLOG}"
                if worker_config.WORKER_ROLE == "overflow" else ""))
    stats.start_reporter()

    camera_cache.reload_cameras()  # первичная загрузка перед стартом основного цикла
    _start_daemon(camera_cache.reload_loop, "camera-reload")
    _start_daemon(persons_reset.reset_loop, "persons-reset")

    # Не блокируем обработку кадров из-за медленного HR API: первичная
    # синхронизация идёт в фоне, задачи от api — в отдельном listener.
    _start_daemon(employee_sync.sync_employees, "employee-initial-sync")
    _start_daemon(employee_jobs.listen_loop, "employee-jobs")

    stream_consumer.consume_forever(crossings.process_frame)


if __name__ == "__main__":
    main()
