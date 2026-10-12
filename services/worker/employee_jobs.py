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
from worker_config import EMPLOYEE_JOB_RESULT_TTL, SOCK_TIMEOUT

log = logging.getLogger("worker")

# Сколько секунд BLPOP ждёт задачу на стороне Redis. Должно быть заметно
# меньше таймаута сокета (SOCK_TIMEOUT), иначе клиент оборвёт чтение раньше,
# чем Redis успеет ответить "задач нет", и получит TimeoutError.
POLL_TIMEOUT_S = 2
RETRY_DELAY_S = 2
assert POLL_TIMEOUT_S < SOCK_TIMEOUT


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


def poll_once(queues: list[str]) -> None:
    """Ждёт одну задачу (до POLL_TIMEOUT_S) и выполняет её.

    Сетевые ошибки Redis (обрыв соединения, таймаут чтения) не пробрасываются:
    логируем и даём listen_loop повторить попытку.
    """
    try:
        job = redis_conn.r.blpop(queues, timeout=POLL_TIMEOUT_S)
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
        log.warning(f"[employee_listener] Redis connection lost: {e}, retrying in {RETRY_DELAY_S}s...")
        time.sleep(RETRY_DELAY_S)
        return
    if job is None:
        return
    queue_raw, payload_raw = job
    handle_job(queue_raw.decode(), payload_raw)


def listen_loop() -> None:
    """Бесконечный цикл приёма задач. Поток не должен умирать: любая
    неожиданная ошибка логируется, и цикл продолжается."""
    queues = list(_HANDLERS)
    while True:
        try:
            poll_once(queues)
        except Exception:
            log.exception(f"[employee_listener] Неожиданная ошибка, повтор через {RETRY_DELAY_S}s")
            time.sleep(RETRY_DELAY_S)
