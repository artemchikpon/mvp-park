"""Единственный Redis-клиент воркера.

Модули обращаются к нему как ``redis_conn.r`` (а не ``from redis_conn import r``),
чтобы тесты могли подменить клиент в одном месте.
"""

from core.redis_utils import make_redis
from config import SOCK_TIMEOUT

r = make_redis(socket_timeout=SOCK_TIMEOUT, retry_on_timeout=True)
