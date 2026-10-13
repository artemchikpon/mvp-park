"""Хранилище embedding'ов лиц: сотрудники (игнорируются) и галерея persons.

Векторы лежат в PostgreSQL как bytes. Чтобы не читать всю таблицу на каждый
кадр, воркер держит in-memory snapshot, который обновляется раз в
CACHE_TTL_SECONDS и сбрасывается при вставке нового Person / изменении
сотрудников (invalidate_cache).

Для конкурентного INSERT используется PostgreSQL advisory lock по хэшу
вектора; в SQLite (тесты) — только локальный lock процесса.
"""

import hashlib
import threading
import time
from dataclasses import dataclass

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from core import perf
from core.timeutil import utcnow
from db.database import SessionLocal
from db.models import Employee, Person
from embeddings import from_bytes, to_bytes

SIMILARITY = 0.65
CACHE_TTL_SECONDS = 5.0

stats = perf.get("worker")


def _lock_key(emb: np.ndarray) -> int:
    """Стабильный signed int64 для pg_advisory_xact_lock."""
    digest = hashlib.sha256(to_bytes(emb)).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


def _similarity(a: np.ndarray, b: np.ndarray) -> float:
    return cosine_similarity([a], [b])[0][0]


@dataclass(frozen=True)
class _Snapshot:
    employees: tuple[np.ndarray, ...] = ()
    persons: tuple[tuple[int, np.ndarray], ...] = ()


class FaceStoragePG:
    _cache_lock = threading.RLock()
    _local_identify_lock = threading.RLock()
    _snapshot = _Snapshot()
    _cache_loaded_at = 0.0

    # ── кэш ──────────────────────────────────────────────────────────────────

    @classmethod
    def invalidate_cache(cls) -> None:
        with cls._cache_lock:
            cls._cache_loaded_at = 0.0
            cls._snapshot = _Snapshot()

    @classmethod
    def _load_snapshot(cls, session) -> "_Snapshot":
        # SQLite используется только в тестах и не имеет общей инвалидации
        # между независимыми Session, поэтому там всегда читаем актуальные
        # строки. В PostgreSQL кэш с TTL резко снижает нагрузку на БД.
        now = time.monotonic()
        with cls._cache_lock:
            fresh = now - cls._cache_loaded_at < CACHE_TTL_SECONDS
            if session.bind.dialect.name != "sqlite" and fresh:
                return cls._snapshot

            with stats.measure("gallery_reload"):
                cls._snapshot = _Snapshot(
                    employees=tuple(from_bytes(emp.embedding) for emp in session.query(Employee).all()),
                    persons=tuple((p.id, from_bytes(p.embedding)) for p in session.query(Person).all()),
                )
            stats.gauge("gallery_persons", len(cls._snapshot.persons))
            stats.gauge("gallery_employees", len(cls._snapshot.employees))
            cls._cache_loaded_at = now
            return cls._snapshot

    @staticmethod
    def _acquire_identity_lock(session, emb: np.ndarray) -> None:
        """Postgres advisory lock; SQLite/unit tests используют локальный lock."""
        if session.bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _lock_key(emb)},
            )

    # ── публичный API ─────────────────────────────────────────────────────────

    def is_employee(self, emb: np.ndarray, session) -> bool:
        employees = self._load_snapshot(session).employees
        with stats.measure("match"):
            return any(_similarity(emb, stored) >= SIMILARITY for stored in employees)

    def identify(self, emb: np.ndarray, face) -> dict | None:
        """Сопоставляет лицо с галереей.

        Возвращает None, если это сотрудник; иначе
        ``{"id", "new", "age", "gender"}`` — найденный или только что
        созданный Person.
        """
        session = SessionLocal()
        # SQLite-тесты не имеют распределённой блокировки, поэтому хотя бы
        # сериализуем identify внутри процесса. В production основной lock —
        # PostgreSQL advisory lock ниже.
        with self._local_identify_lock:
            try:
                with stats.measure("db_person"):
                    self._acquire_identity_lock(session, emb)

                if self.is_employee(emb, session):
                    return None

                match = self._best_person_match(emb, session)
                if match is not None:
                    return self._touch_existing(session, match, emb, face)
                return self._create_person(session, emb, face)
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    # ── внутреннее ────────────────────────────────────────────────────────────

    def _best_person_match(self, emb: np.ndarray, session) -> int | None:
        persons = self._load_snapshot(session).persons
        best_id = None
        best_score = 0.0
        with stats.measure("match"):
            for person_id, stored in persons:
                score = _similarity(emb, stored)
                if score > best_score:  # строго больше: при равенстве — первый
                    best_score = score
                    best_id = person_id
        if best_id is not None and best_score >= SIMILARITY:
            return best_id
        return None

    def _touch_existing(self, session, person_id: int, emb, face) -> dict:
        with stats.measure("db_person"):
            p = session.get(Person, person_id)
        if p is None:
            # Запись удалили (например, ежедневный сброс) после снимка кэша.
            self.invalidate_cache()
            return self.identify(emb, face)

        # Эталонный embedding не изменяем: иначе два разных лица с
        # пограничным similarity со временем слились бы в одну личность
        # из-за скользящего среднего.
        p.last_seen = utcnow()
        with stats.measure("db_person"):
            session.commit()
        return {"id": person_id, "new": False, "age": p.age, "gender": p.gender}

    def _create_person(self, session, emb, face) -> dict:
        age = int(face.age) if face.age is not None else -1
        gender = int(face.gender) if face.gender is not None else -1
        person = Person(embedding=to_bytes(emb), age=age, gender=gender)
        session.add(person)
        try:
            with stats.measure("db_person"):
                session.commit()
        except IntegrityError:
            # Защита от редкого race/дубликата на уровне БД: повторяем
            # поиск уже существующей записи после rollback.
            session.rollback()
            self.invalidate_cache()
            return self.identify(emb, face)

        with stats.measure("db_person"):
            session.refresh(person)
        self.invalidate_cache()
        return {"id": person.id, "new": True, "age": age, "gender": gender}
