"""Создание Redis-клиента из окружения и ожидание готовности Redis."""

import logging
import time

from core.env import env_int, env_str

log = logging.getLogger("db")


def make_redis(*, decode_responses: bool = False, socket_timeout: float = 5,
               retry_on_timeout: bool | None = None):
    """Redis-клиент по REDIS_HOST/REDIS_PORT.

    Конструктор ленивый: соединение открывается при первой команде, поэтому
    вызывать на уровне модуля безопасно (в т.ч. в тестах).
    retry_on_timeout передаётся в redis-py, только если задан явно.
    """
    import redis

    kwargs = {}
    if retry_on_timeout is not None:
        kwargs["retry_on_timeout"] = retry_on_timeout
    return redis.Redis(
        host=env_str("REDIS_HOST", "redis"),
        port=env_int("REDIS_PORT", 6379),
        decode_responses=decode_responses,
        socket_timeout=socket_timeout,
        socket_connect_timeout=5,
        **kwargs,
    )


def wait_for_redis(host: str = "redis", port: int = 6379,
                   retries: int = 15, delay: int = 2) -> None:
    """Ждёт, пока Redis не начнёт отвечать на PING.

    NB: хост/порт берутся из аргументов (по умолчанию redis:6379), а не из
    REDIS_HOST/REDIS_PORT — так было в исходной версии (db.database).
    """
    import redis as redis_lib

    for attempt in range(1, retries + 1):
        try:
            redis_lib.Redis(host=host, port=port).ping()
            log.info("Redis ready.")
            return
        except Exception as e:
            log.warning(f"Redis not ready (attempt {attempt}/{retries}): {e}")
            time.sleep(delay)
    raise RuntimeError("Cannot connect to Redis after multiple retries.")
