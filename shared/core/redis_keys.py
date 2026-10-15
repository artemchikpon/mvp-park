"""Имена Redis-ключей/стримов, общие для нескольких сервисов.

Значения менять нельзя без одновременного деплоя всех сервисов: producer
пишет в FRAMES_STREAM, api кладёт задачи в очереди employee_*, worker их
читает и отвечает в *_RESULT_PREFIX:{job_id}.
"""

# producer -> worker
FRAMES_STREAM = "frames"
FRAMES_CONSUMER_GROUP = "workers"

# api -> worker: задачи по сотрудникам (RPC поверх списков Redis)
EMPLOYEE_SYNC_JOBS = "employee_sync_jobs"
EMPLOYEE_ADD_JOBS = "employee_add_jobs"
EMPLOYEE_SYNC_RESULT_PREFIX = "employee_sync_result"
EMPLOYEE_ADD_RESULT_PREFIX = "employee_add_result"

# worker: служебные ключи
CROSSING_COOLDOWN_PREFIX = "crossing_cooldown"
PERSONS_RESET_LOCK = "persons_reset_lock"


def result_key(prefix: str, job_id: str) -> str:
    return f"{prefix}:{job_id}"
