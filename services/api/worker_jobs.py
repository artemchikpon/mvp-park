"""RPC к воркеру через Redis для операций, которым нужен InsightFace.

api кладёт JSON-задачу в список-очередь и ждёт ответ BLPOP-ом по ключу
``<result_prefix>:<job_id>`` (протокол — см. services/worker/employees/jobs.py).

Функция не знает про HTTP: роуты сами переводят исключения в ответы.
"""

import json
import uuid

from api.config import EMPLOYEE_JOB_TIMEOUT
from core.redis_keys import result_key
from core.redis_utils import make_redis


class EnqueueError(Exception):
    """Не удалось положить задачу в очередь (Redis недоступен)."""


class WaitError(Exception):
    """Ошибка Redis во время ожидания ответа."""


class WorkerTimeout(Exception):
    """Воркер не ответил за EMPLOYEE_JOB_TIMEOUT секунд."""


class WorkerJobError(Exception):
    """Воркер выполнил задачу, но вернул {"error": ...}."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def run_worker_job(queue: str, result_prefix: str, payload: dict | None = None) -> dict:
    """Ставит задачу и синхронно ждёт ответ воркера.

    Исключения Redis оборачиваются в EnqueueError / WaitError (исходное — в
    __cause__), отсутствие ответа -> WorkerTimeout, {"error"} -> WorkerJobError.
    """
    import redis

    job_id = str(uuid.uuid4())
    client = make_redis(decode_responses=True, socket_timeout=EMPLOYEE_JOB_TIMEOUT + 5)

    try:
        client.rpush(queue, json.dumps({"job_id": job_id, **(payload or {})}))
    except redis.exceptions.RedisError as e:
        raise EnqueueError(str(e)) from e

    try:
        response = client.blpop(result_key(result_prefix, job_id), timeout=EMPLOYEE_JOB_TIMEOUT)
    except redis.exceptions.RedisError as e:
        raise WaitError(str(e)) from e

    if response is None:
        raise WorkerTimeout()

    _, raw = response
    data = json.loads(raw)
    if "error" in data:
        raise WorkerJobError(data["error"])
    return data
