"""Хранилище embedding'ов лиц.

Векторы остаются в PostgreSQL как bytes для совместимости с текущей схемой.
Для production воркер держит небольшой in-memory snapshot, чтобы не читать
всю таблицу на каждый кадр. Snapshot периодически обновляется, а вставка
нового Person инвалидирует его.

Для конкурентного INSERT используется PostgreSQL advisory lock: несколько
реплик worker не смогут одновременно создать Person для одного и того же
embedding.
"""

import hashlib
import threading
import time

import numpy as np
from datetime import datetime
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from db.database import SessionLocal
from db.models import Person, Employee

SIMILARITY = 0.65
EMB_DTYPE = np.float32
CACHE_TTL_SECONDS = 5.0


def _to_bytes(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=EMB_DTYPE).tobytes()


def _from_bytes(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=EMB_DTYPE)


def _lock_key(emb: np.ndarray) -> int:
    """Стабильный signed int64 для pg_advisory_xact_lock."""
    digest = hashlib.sha256(_to_bytes(emb)).digest()[:8]
    value = int.from_bytes(digest, byteorder="big", signed=True)
    return value


class FaceStoragePG:
    _cache_lock = threading.RLock()
    _local_identify_lock = threading.RLock()
    _persons_cache: list[tuple[int, np.ndarray]] = []
    _employees_cache: list[np.ndarray] = []
    _cache_loaded_at = 0.0

    @classmethod
    def invalidate_cache(cls) -> None:
        with cls._cache_lock:
            cls._cache_loaded_at = 0.0
            cls._persons_cache = []
            cls._employees_cache = []

    @classmethod
    def _refresh_cache(cls, session) -> None:
        # SQLite используется только в тестах/локальной разработке и не
        # имеет общего cache invalidation между независимыми Session.
        # Поэтому там всегда читаем актуальные строки. В PostgreSQL cache
        # работает с TTL и резко снижает нагрузку на БД.
        now = time.monotonic()
        with cls._cache_lock:
            if session.bind.dialect.name != "sqlite" and now - cls._cache_loaded_at < CACHE_TTL_SECONDS:
                return
            cls._employees_cache = [
                _from_bytes(emp.embedding) for emp in session.query(Employee).all()
            ]
            cls._persons_cache = [
                (p.id, _from_bytes(p.embedding)) for p in session.query(Person).all()
            ]
            cls._cache_loaded_at = now

    @classmethod
    def _acquire_identity_lock(cls, session, emb: np.ndarray) -> None:
        """Postgres advisory lock; SQLite/unit tests используют локальный lock."""
        if session.bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _lock_key(emb)},
            )

    def is_employee(self, emb: np.ndarray, session) -> bool:
        self._refresh_cache(session)
        with self._cache_lock:
            employees = list(self._employees_cache)

        for stored in employees:
            score = cosine_similarity([emb], [stored])[0][0]
            if score >= SIMILARITY:
                return True
        return False

    def identify(self, emb: np.ndarray, face) -> dict | None:
        session = SessionLocal()
        # SQLite тесты не имеют распределённой блокировки, поэтому хотя бы
        # сериализуем identify внутри процесса. В production основной lock —
        # PostgreSQL advisory lock ниже.
        with self._local_identify_lock:
            try:
                self._acquire_identity_lock(session, emb)

                if self.is_employee(emb, session):
                    return None

                self._refresh_cache(session)
                with self._cache_lock:
                    persons = list(self._persons_cache)

                best_id = None
                best_score = 0.0
                for person_id, stored in persons:
                    score = cosine_similarity([emb], [stored])[0][0]
                    if score > best_score:
                        best_score = score
                        best_id = person_id

                age = int(face.age) if face.age is not None else -1
                gender = int(face.gender) if face.gender is not None else -1

                if best_id is not None and best_score >= SIMILARITY:
                    p = session.get(Person, best_id)
                    if p is None:
                        self.invalidate_cache()
                        return self.identify(emb, face)

                    # Не изменяем эталонный embedding. Иначе два разных лица
                    # с пограничным similarity со временем могут слиться в одну
                    # личность из-за скользящего среднего.
                    p.last_seen = datetime.utcnow()
                    session.commit()
                    return {"id": best_id, "new": False, "age": p.age, "gender": p.gender}

                person = Person(
                    embedding=_to_bytes(emb),
                    age=age,
                    gender=gender,
                )
                session.add(person)
                try:
                    session.commit()
                except IntegrityError:
                    # Защита от редкого race/дубликата на уровне БД: повторяем
                    # поиск уже существующей записи после rollback.
                    session.rollback()
                    self.invalidate_cache()
                    return self.identify(emb, face)

                session.refresh(person)
                self.invalidate_cache()
                return {"id": person.id, "new": True, "age": age, "gender": gender}
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
