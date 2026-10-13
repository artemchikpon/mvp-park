"""Точка входа worker-сервиса.

Модули:
  stream_consumer — чтение кадров из Redis Stream (consumer group)
  crossings       — кадр -> лица -> идентификация -> событие пересечения
  camera_cache    — кэш настроек камер
  persons_reset   — ежедневная очистка галереи persons
  employee_jobs   — задачи по сотрудникам от api
  employee_sync   — синхронизация сотрудников с HR API
"""

import logging
import threading

import camera_cache
import crossings
import employee_jobs
import employee_sync
import persons_reset
import stream_consumer
from core import perf
from core.redis_utils import wait_for_redis
from db.database import wait_for_db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("worker")


def _start_daemon(target, name: str) -> None:
    threading.Thread(target=target, name=name, daemon=True).start()


def main() -> None:
    wait_for_db()
    wait_for_redis()
    stream_consumer.ensure_consumer_group()
    perf.get("worker").start_reporter()

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
