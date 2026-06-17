"""
FaceStorage поверх PostgreSQL.

Логика:
  - При identify() ищем похожее лицо среди Person в БД.
  - Если нашли (cosine ≥ SIMILARITY) — обновляем embedding (скользящее среднее),
    last_seen. Это НЕ новое лицо.
  - Не нашли — INSERT. Это НОВОЕ лицо.

Embedding хранится как bytes (numpy float32, 512-dim → 2048 bytes).
"""

import numpy as np
from datetime import datetime
from sklearn.metrics.pairwise import cosine_similarity

from db.database import SessionLocal
from db.models import Person

SIMILARITY = 0.65
EMB_DTYPE = np.float32


def _to_bytes(vec: np.ndarray) -> bytes:
    return vec.astype(EMB_DTYPE).tobytes()


def _from_bytes(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=EMB_DTYPE)


class FaceStoragePG:

    def identify(self, emb: np.ndarray, face) -> dict:
        """
        Возвращает dict:
          id, new (bool), age, gender
        """
        session = SessionLocal()
        try:
            persons = session.query(Person).all()

            best_id = None
            best_score = 0.0

            for p in persons:
                stored = _from_bytes(p.embedding)
                score = cosine_similarity([emb], [stored])[0][0]
                if score > best_score:
                    best_score = score
                    best_id = p.id

            age = int(face.age) if face.age is not None else -1
            gender = int(face.gender) if face.gender is not None else -1

            if best_id and best_score >= SIMILARITY:
                # ── Знакомое лицо ─────────────────────────────────────────
                p = session.get(Person, best_id)

                # Скользящее среднее embedding: простое среднее (old + new) / 2
                old_vec = _from_bytes(p.embedding)
                new_vec = (old_vec + emb) / 2
                p.embedding = _to_bytes(new_vec)

                p.last_seen = datetime.utcnow()
                session.commit()

                return {"id": best_id, "new": False, "age": p.age, "gender": p.gender}

            else:
                # ── Новое лицо ────────────────────────────────────────────
                person = Person(
                    embedding=_to_bytes(emb),
                    age=age,
                    gender=gender,
                )
                session.add(person)
                session.commit()
                session.refresh(person)

                return {"id": person.id, "new": True, "age": age, "gender": gender}

        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()
