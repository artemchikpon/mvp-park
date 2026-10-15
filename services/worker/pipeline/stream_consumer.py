"""Чтение стрима кадров через Redis consumer group.

Consumer group раздаёт каждое сообщение ровно одному консьюмеру, поэтому
несколько реплик воркера делят кадры между собой, а не дублируют работу.
Сообщение подтверждается (XACK) только после успешной обработки; если
реплика упала посреди кадра, сообщение остаётся в PEL и через CLAIM_IDLE_MS
его подбирает другая реплика (XAUTOCLAIM).

Роль overflow (WORKER_ROLE=overflow, обычно CPU-воркер рядом с GPU-воркером):
новые кадры берутся, только если в очереди накопилось не меньше
OVERFLOW_MIN_BACKLOG ещё никому не выданных кадров. Пока GPU-воркер
справляется, очередь пуста и CPU не тратится; если GPU не успевает или упал,
очередь растёт и overflow-воркер подключается сам.
"""

import logging
import time
from typing import Callable

import redis

import redis_conn
from core.redis_keys import FRAMES_CONSUMER_GROUP, FRAMES_STREAM
from core import perf
from config import (
    BLOCK_MS, CLAIM_IDLE_MS, CLAIM_INTERVAL_S, CONSUMER_NAME, OVERFLOW_IDLE_SLEEP_S,
    OVERFLOW_MIN_BACKLOG, WORKER_ROLE,
)

log = logging.getLogger("worker")

Handler = Callable[[bytes, dict], None]


def ensure_consumer_group() -> None:
    try:
        redis_conn.r.xgroup_create(FRAMES_STREAM, FRAMES_CONSUMER_GROUP, id="$", mkstream=True)
        log.info(f"Создана consumer group '{FRAMES_CONSUMER_GROUP}' на стриме '{FRAMES_STREAM}'")
    except redis.exceptions.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise
        log.info(f"Consumer group '{FRAMES_CONSUMER_GROUP}' уже существует")


def _handle_and_ack(handler: Handler, message_id: bytes, data: dict) -> None:
    try:
        handler(message_id, data)
        redis_conn.r.xack(FRAMES_STREAM, FRAMES_CONSUMER_GROUP, message_id)
    except Exception:
        log.exception(f"Ошибка обработки сообщения {message_id}; "
                      f"сообщение останется в PEL для повторной обработки")


def _read_new(handler: Handler) -> None:
    try:
        # ">" — только новые, ещё никому не выданные сообщения
        messages = redis_conn.r.xreadgroup(
            FRAMES_CONSUMER_GROUP, CONSUMER_NAME, {FRAMES_STREAM: ">"},
            block=BLOCK_MS, count=1,
        )
    except redis.exceptions.TimeoutError:
        return
    if messages:
        _, entries = messages[0]
        for message_id, data in entries:
            _handle_and_ack(handler, message_id, data)


def _claim_stale(handler: Handler) -> None:
    """Забирает себе сообщения, зависшие у упавших/застрявших реплик."""
    cursor = b"0-0"
    while True:
        next_cursor, claimed, _ = redis_conn.r.xautoclaim(
            FRAMES_STREAM, FRAMES_CONSUMER_GROUP, CONSUMER_NAME,
            min_idle_time=CLAIM_IDLE_MS, start_id=cursor, count=10,
        )
        for message_id, data in claimed:
            _handle_and_ack(handler, message_id, data)
        if not claimed or next_cursor in (b"0-0", "0-0"):
            return
        cursor = next_cursor


def undelivered_count(limit: int) -> int:
    """Сколько кадров в стриме ещё не выдано ни одному консьюмеру группы
    (не больше limit — точнее не нужно). Основной источник — поле lag из
    XINFO GROUPS (Redis >= 7); если Redis не может его посчитать (None),
    считаем XRANGE после last-delivered-id."""
    for group in redis_conn.r.xinfo_groups(FRAMES_STREAM):
        name = group.get("name")
        if name not in (FRAMES_CONSUMER_GROUP, FRAMES_CONSUMER_GROUP.encode()):
            continue
        lag = group.get("lag")
        if lag is not None:
            return min(int(lag), limit)
        last = group.get("last-delivered-id")
        last = last.decode() if isinstance(last, bytes) else last
        return len(redis_conn.r.xrange(FRAMES_STREAM, min=f"({last}", max="+", count=limit))
    return 0


def _should_read_new() -> bool:
    if WORKER_ROLE != "overflow":
        return True
    if undelivered_count(OVERFLOW_MIN_BACKLOG) >= OVERFLOW_MIN_BACKLOG:
        perf.get("worker").count("overflow_active_polls")
        return True
    return False


def consume_forever(handler: Handler) -> None:
    last_claim_check = 0.0
    while True:
        try:
            if _should_read_new():
                _read_new(handler)
            else:
                time.sleep(OVERFLOW_IDLE_SLEEP_S)
        except redis.exceptions.ConnectionError as e:
            log.warning(f"Redis connection lost: {e}, reconnecting in 2s...")
            time.sleep(2)
            continue

        now = time.time()
        if now - last_claim_check > CLAIM_INTERVAL_S:
            last_claim_check = now
            try:
                _claim_stale(handler)
            except redis.exceptions.ConnectionError as e:
                log.warning(f"Redis connection lost during XAUTOCLAIM: {e}")
