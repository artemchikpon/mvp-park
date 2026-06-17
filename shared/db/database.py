import os
import time
import logging
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import OperationalError

log = logging.getLogger("db")

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://face:face@postgres:5432/face_db"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine)

Base = declarative_base()


def wait_for_db(retries: int = 15, delay: int = 2) -> None:
    """Ждёт, пока PostgreSQL не начнёт принимать соединения."""
    for attempt in range(1, retries + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            log.info("PostgreSQL ready.")
            return
        except OperationalError as e:
            log.warning(f"DB not ready (attempt {attempt}/{retries}): {e.orig}")
            time.sleep(delay)
    raise RuntimeError("Cannot connect to PostgreSQL after multiple retries.")


def wait_for_redis(host: str = "redis", port: int = 6379,
                   retries: int = 15, delay: int = 2) -> None:
    """Ждёт, пока Redis не начнёт отвечать на PING."""
    import redis as redis_lib
    for attempt in range(1, retries + 1):
        try:
            r = redis_lib.Redis(host=host, port=port)
            r.ping()
            log.info("Redis ready.")
            return
        except Exception as e:
            log.warning(f"Redis not ready (attempt {attempt}/{retries}): {e}")
            time.sleep(delay)
    raise RuntimeError("Cannot connect to Redis after multiple retries.")
