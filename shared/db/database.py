import logging
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker

from core.env import env_str

log = logging.getLogger("db")

DATABASE_URL = env_str("DATABASE_URL", "postgresql://face:face@postgres:5432/face_db")

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
