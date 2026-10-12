"""Обработка задач по сотрудникам, которые ставит api-сервис.

В api нет InsightFace, поэтому он кладёт задачу в Redis-список
(EMPLOYEE_SYNC_JOBS / EMPLOYEE_ADD_JOBS), любая реплика воркера забирает её
BLPOP-ом, выполняет и кладёт JSON-ответ в ``<prefix>:<job_id>``, где его
ждёт api. При ошибке ответ имеет вид ``{"error": "..."}``.
"""

import json
import logging
import time

import redis

import employee_sync
import redis_conn
from core.redis_keys import (
    EMPLOYEE_ADD_JOBS, EMPLOYEE_ADD_RESULT_PREFIX, EMPLOYEE_SYNC_JOBS,
    EMPLOYEE_SYNC_RESULT_PREFIX, result_key,
)
from worker_config import EMPLOYEE_JOB_RESULT_TTL

log = logging.getLogger("worker")

POLL_TIMEOUT_S = 5


def _parse_payload(raw: bytes) -> dict:
    payload = raw.decode()
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        # Совместимость с задачами, поставленными старой версией API
        # (в очереди лежал просто job_id строкой).
        return {"job_id": payload}


def _run_sync(data: dict) -> dict:
    return employee_sync.sync_employees()


def _run_add(data: dict) -> dict:
    return employee_sync.add_employee_from_file(
        first_name=data["first_name"],
        file_id=int(data["file_id"]),
        external_id=data.get("external_id"),
    )


# очередь -> (текст "получена задача ...", текст "ошибка ...", обработчик, префикс ответа)
_HANDLERS = {
    EMPLOYEE_SYNC_JOBS: ("полной синхронизации", "синхронизации", _run_sync, EMPLOYEE_SYNC_RESULT_PREFIX),
    EMPLOYEE_ADD_JOBS: ("добавления сотрудника", "добавления", _run_add, EMPLOYEE_ADD_RESULT_PREFIX),
}


def handle_job(queue: str, raw_payload: bytes) -> None:
    data = _parse_payload(raw_payload)
    job_id = data["job_id"]
    title, error_title, handler, prefix = _HANDLERS[queue]

    log.info(f"[employee_listener] Получена задача {title} job_id={job_id}")
    try:
        result = handler(data)
    except Exception as e:
        log.error(f"[employee_listener] Ошибка {error_title} job_id={job_id}: {e}")
        result = {"error": str(e)}

    key = result_key(prefix, job_id)
    try:
        redis_conn.r.rpush(key, json.dumps(result))
        redis_conn.r.expire(key, EMPLOYEE_JOB_RESULT_TTL)
    except redis.exceptions.ConnectionError as e:
        log.error(f"[employee_sync_listener] Не удалось записать результат job_id={job_id}: {e}")


def listen_loop() -> None:
    queues = list(_HANDLERS)
    while True:
        try:
            job = redis_conn.r.blpop(queues, timeout=POLL_TIMEOUT_S)
        except redis.exceptions.ConnectionError as e:
            log.warning(f"[employee_listener] Redis connection lost: {e}, retrying in 2s...")
            time.sleep(2)
            continue
        if job is None:
            continue
        queue_raw, payload_raw = job
        handle_job(queue_raw.decode(), payload_raw)
