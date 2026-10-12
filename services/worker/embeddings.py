"""Сериализация embedding-векторов лиц.

В БД векторы хранятся как сырые bytes float32 (Person.embedding,
Employee.embedding).
"""

import numpy as np

EMB_DTYPE = np.float32


def to_bytes(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=EMB_DTYPE).tobytes()


def from_bytes(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=EMB_DTYPE)
