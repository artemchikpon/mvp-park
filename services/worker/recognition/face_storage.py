"""Сопоставление лица с базой: сотрудники (игнорируются) и галерея persons.

Векторы лиц лежат в PostgreSQL в колонках pgvector `vector(512)`, и поиск
самого похожего идёт прямо в базе одним запросом:

    SELECT id, embedding <=> :face AS distance
    FROM persons ORDER BY distance, id LIMIT 1

`<=>` — косинусное расстояние, сходство = 1 - расстояние (та же метрика,
что cosine_similarity раньше). Поиск точный (перебор всех строк, без
приблизительного индекса): для галереи на несколько тысяч человек за день
это единицы миллисекунд. Если галерея вырастет до сотен тысяч, можно
добавить индекс HNSW — см. README.

Копии галереи в памяти воркера больше нет: все реплики ищут по одной
таблице и сразу видят только что добавленных людей. Чтобы две реплики не
завели одного и того же человека одновременно, "поиск + вставка" идут под
общим PostgreSQL advisory lock (в SQLite-тестах — локальный lock процесса).
"""

import threading

import numpy as np
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from core import perf
from core.timeutil import utcnow
from db.database import SessionLocal
from db.models import Employee, Person

SIMILARITY = 0.65

# Ключ pg_advisory_xact_lock для identify (произвольная константа проекта).
IDENTIFY_LOCK_KEY = 7_405_190_001

stats = perf.get("worker")


def _as_vector(emb) -> np.ndarray:
    return np.asarray(emb, dtype=np.float32)


def _cosine(matrix: np.ndarray, emb: np.ndarray) -> np.ndarray:
    """Косинусное сходство строк matrix с emb (нулевой вектор -> 0)."""
    norms = np.linalg.norm(matrix, axis=1) * np.linalg.norm(emb)
    dots = matrix @ emb
    return np.divide(dots, norms, out=np.zeros_like(dots), where=norms > 0)


def nearest(session, model, emb) -> tuple[int, float] | None:
    """(id, сходство) самой похожей строки model; None — таблица пуста.

    При равном сходстве — строка с меньшим id.
    """
    emb = _as_vector(emb)
    if session.bind.dialect.name == "postgresql":
        distance = model.embedding.cosine_distance(emb)
        row = session.query(model.id, distance).order_by(distance, model.id).first()
        return None if row is None else (row[0], 1.0 - float(row[1]))

    # SQLite (тесты): pgvector там нет — тот же косинус в numpy.
    rows = session.query(model.id, model.embedding).order_by(model.id).all()
    if not rows:
        return None
    scores = _cosine(np.stack([_as_vector(r[1]) for r in rows]), emb)
    best = int(np.argmax(scores))  # argmax берёт первый максимум -> меньший id
    return rows[best][0], float(scores[best])


class FaceStoragePG:
    _local_identify_lock = threading.RLock()

    @staticmethod
    def _acquire_identity_lock(session) -> None:
        """Один identify за раз на всю систему (в рамках транзакции): иначе
        две реплики могли бы одновременно не найти человека и обе создать
        его. Держится миллисекунды — поиск и вставка."""
        if session.bind.dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": IDENTIFY_LOCK_KEY})

    # ── публичный API ─────────────────────────────────────────────────────────

    def is_employee(self, emb, session) -> bool:
        with stats.measure("match"):
            found = nearest(session, Employee, emb)
        return found is not None and found[1] >= SIMILARITY

    def identify(self, emb, face) -> dict | None:
        """Сопоставляет лицо с галереей.

        Возвращает None, если это сотрудник; иначе
        ``{"id", "new", "age", "gender"}`` — найденный или только что
        созданный Person.
        """
        session = SessionLocal()
        # SQLite-тесты не имеют распределённой блокировки, поэтому хотя бы
        # сериализуем identify внутри процесса. В production основной lock —
        # PostgreSQL advisory lock.
        with self._local_identify_lock:
            try:
                with stats.measure("db_person"):
                    self._acquire_identity_lock(session)

                if self.is_employee(emb, session):
                    session.rollback()  # снять advisory lock
                    return None

                with stats.measure("match"):
                    found = nearest(session, Person, emb)
                if found is not None and found[1] >= SIMILARITY:
                    return self._touch_existing(session, found[0], emb, face)
                return self._create_person(session, emb, face)
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    # ── внутреннее ────────────────────────────────────────────────────────────

    def _touch_existing(self, session, person_id: int, emb, face) -> dict:
        with stats.measure("db_person"):
            p = session.get(Person, person_id)
        if p is None:
            # Запись удалили между поиском и чтением (ежедневный сброс).
            return self._create_person(session, emb, face)

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
        person = Person(embedding=_as_vector(emb), age=age, gender=gender)
        session.add(person)
        try:
            with stats.measure("db_person"):
                session.commit()
        except IntegrityError:
            # Защита от редкого сбоя на уровне БД: повторяем identify с нуля.
            session.rollback()
            return self.identify(emb, face)

        with stats.measure("db_person"):
            session.refresh(person)
        return {"id": person.id, "new": True, "age": age, "gender": gender}
