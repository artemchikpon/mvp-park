"""Ежедневный сброс галереи persons.

Продуктовое требование: галерея векторов уникальных лиц должна каждый день
начинаться "с нуля" — сбрасываем её раз в сутки в PERSONS_RESET_HOUR:MINUTE
по таймзоне парка (park_settings.timezone).

Реплик воркера несколько, и все просыпаются в одну минуту. Redis-лок
(SET NX + TTL) на дату гарантирует, что реально удалит записи только одна.
"""

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import redis

import redis_conn
from core.redis_keys import PERSONS_RESET_LOCK
from core.timeutil import DEFAULT_PARK_TIMEZONE
from db import stats_service
from db.database import SessionLocal
from face_storage_pg import FaceStoragePG
from worker_config import (
    CONSUMER_NAME, PERSONS_RESET_HOUR, PERSONS_RESET_LOCK_TTL, PERSONS_RESET_MINUTE,
)

log = logging.getLogger("worker")

RETRY_DELAY_S = 60


def park_timezone() -> ZoneInfo:
    """Таймзона парка. При любой ошибке — таймзона по умолчанию, чтобы поток
    сброса не падал."""
    session = SessionLocal()
    try:
        return ZoneInfo(stats_service.get_settings(session).timezone)
    except Exception as e:
        log.warning(f"[persons_reset] Не удалось прочитать таймзону парка, "
                    f"использую {DEFAULT_PARK_TIMEZONE}: {e}")
        return ZoneInfo(DEFAULT_PARK_TIMEZONE)
    finally:
        session.close()


def lock_key_for(day_iso: str) -> str:
    return f"{PERSONS_RESET_LOCK}:{day_iso}"


def seconds_until_next_reset() -> float:
    now = datetime.now(park_timezone())
    target = now.replace(hour=PERSONS_RESET_HOUR, minute=PERSONS_RESET_MINUTE,
                         second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def run_once() -> None:
    today = datetime.now(park_timezone()).date().isoformat()
    lock_key = lock_key_for(today)

    if not redis_conn.r.set(lock_key, CONSUMER_NAME, ex=PERSONS_RESET_LOCK_TTL, nx=True):
        log.info("[persons_reset] Сброс persons на сегодня уже выполнен другой репликой")
        return

    session = SessionLocal()
    try:
        deleted = stats_service.reset_persons(session)
        # In-memory кэш векторов ещё до CACHE_TTL_SECONDS мог бы отдавать
        # удалённые записи — инвалидируем сразу, чтобы новые лица не
        # "склеивались" со старыми.
        FaceStoragePG.invalidate_cache()
        log.info(
            f"[persons_reset] Таблица persons очищена: удалено {deleted} записей "
            f"(плановый сброс {PERSONS_RESET_HOUR:02d}:{PERSONS_RESET_MINUTE:02d} "
            f"по таймзоне парка)"
        )
    except Exception:
        log.exception("[persons_reset] Ошибка сброса таблицы persons")
        # Снимаем лок, чтобы попытку можно было повторить (иначе до конца
        # дня persons так и останется незачищенной).
        try:
            redis_conn.r.delete(lock_key)
        except redis.exceptions.RedisError:
            log.warning("[persons_reset] Не удалось снять лок после ошибки")
    finally:
        session.close()


def reset_loop() -> None:
    while True:
        try:
            time.sleep(seconds_until_next_reset())
            run_once()
        except Exception:
            log.exception(f"[persons_reset] Ошибка в цикле сброса; повтор через {RETRY_DELAY_S}с")
            time.sleep(RETRY_DELAY_S)
