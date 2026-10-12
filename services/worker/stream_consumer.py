"""Чтение стрима кадров через Redis consumer group.

Consumer group раздаёт каждое сообщение ровно одному консьюмеру, поэтому
несколько реплик воркера делят кадры между собой, а не дублируют работу.
Сообщение подтверждается (XACK) только после успешной обработки; если
реплика упала посреди кадра, сообщение остаётся в PEL и через CLAIM_IDLE_MS
его подбирает другая реплика (XAUTOCLAIM).
"""

import logging
import time
from typing import Callable

import redis

import redis_conn
from core.redis_keys import FRAMES_CONSUMER_GROUP, FRAMES_STREAM
from worker_config import BLOCK_MS, CLAIM_IDLE_MS, CLAIM_INTERVAL_S, CONSUMER_NAME

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


def consume_forever(handler: Handler) -> None:
    last_claim_check = 0.0
    while True:
        try:
            _read_new(handler)
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
