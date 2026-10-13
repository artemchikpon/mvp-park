"""Обработка одного кадра: лица -> идентификация -> событие пересечения.

Несколько реплик воркера читают общий стрим, поэтому cooldown-метки
("этого человека на этой камере уже посчитали") хранятся в Redis.
"""

import logging
import time
import uuid
from collections import Counter

import cv2
import numpy as np
import redis

import camera_cache
import redis_conn
from core import perf
from core.redis_keys import CROSSING_COOLDOWN_PREFIX
from db.stats_service import log_crossing
from face_engine import extract_faces
from face_storage_pg import FaceStoragePG
from worker_config import CROSSING_COOLDOWN_SECONDS

log = logging.getLogger("worker")

# FaceStoragePG ничего не подключает в конструкторе (сессии открываются
# лениво внутри identify()), поэтому безопасно создавать на уровне модуля.
storage = FaceStoragePG()

stats = perf.get("worker")

_RELEASE_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


# ── cooldown ─────────────────────────────────────────────────────────────────

def cooldown_key(camera_id: int, person_id: int) -> str:
    return f"{CROSSING_COOLDOWN_PREFIX}:{camera_id}:{person_id}"


def already_counted_recently(camera_id: int, person_id: int, reserve: bool = True) -> bool:
    """Проверяет cooldown.

    reserve=True — заодно ставит метку (удобно для прямых вызовов).
    process_frame использует reserve=False и резервирует метку отдельно,
    чтобы снять её, если запись в БД не удалась.
    """
    if redis_conn.r.exists(cooldown_key(camera_id, person_id)):
        return True
    if reserve:
        return reserve_cooldown(camera_id, person_id) is None
    return False


def reserve_cooldown(camera_id: int, person_id: int) -> str | None:
    """Атомарно (SET NX) резервирует cooldown и возвращает token владельца,
    либо None, если метку уже поставил кто-то другой."""
    token = uuid.uuid4().hex
    if redis_conn.r.set(cooldown_key(camera_id, person_id), token,
                        ex=CROSSING_COOLDOWN_SECONDS, nx=True):
        return token
    return None


def release_cooldown(camera_id: int, person_id: int, token: str) -> None:
    """Удаляет cooldown, только если ключ всё ещё принадлежит этой попытке."""
    key = cooldown_key(camera_id, person_id)
    try:
        redis_conn.r.eval(_RELEASE_IF_OWNER, 1, key, token)
    except redis.exceptions.RedisError:
        log.warning("Не удалось освободить cooldown %s", key)


# ── кадр ─────────────────────────────────────────────────────────────────────

def _decode_frame(data: dict) -> np.ndarray | None:
    return cv2.imdecode(np.frombuffer(data[b"frame"], np.uint8), cv2.IMREAD_COLOR)


COUNTED, SKIPPED, FAILED = "counted", "skipped", "failed"


def _count_face(camera: camera_cache.CameraInfo, face) -> str:
    """Пытается засчитать одно лицо.

    COUNTED — событие записано; SKIPPED — сотрудник или сработал cooldown;
    FAILED — БД не приняла событие (cooldown снят для повторной попытки).
    """
    res = storage.identify(face.embedding, face)
    if res is None:  # сотрудник
        log.debug("Лицо сотрудника — пропускаем")
        stats.count("employees_seen")
        return SKIPPED
    if res["new"]:
        stats.count("new_persons")

    person_id = res["id"]
    with stats.measure("redis_cooldown"):
        if already_counted_recently(camera.id, person_id, reserve=False):
            return SKIPPED
        token = reserve_cooldown(camera.id, person_id)
    if token is None:
        return SKIPPED

    # Возраст/пол фиксируем по ТЕКУЩЕМУ кадру (в момент пересечения),
    # а не по значению, сохранённому в Person.
    age = int(face.age) if face.age is not None else None
    gender = int(face.gender) if face.gender is not None else None

    with stats.measure("db_event"):
        recorded = log_crossing(camera, person_id, age, gender)
    if recorded:
        stats.count("crossings")
        return COUNTED
    # БД не приняла событие: снимаем cooldown, чтобы следующий кадр мог
    # повторить запись.
    release_cooldown(camera.id, person_id, token)
    return FAILED


def _queue_wait_s(message_id: bytes) -> float | None:
    """Сколько кадр ждал в очереди: ID сообщения Redis Stream начинается с
    времени добавления в миллисекундах ("1791804601170-0")."""
    try:
        added_ms = int(message_id.split(b"-", 1)[0])
    except (ValueError, AttributeError):
        return None
    return max(time.time() - added_ms / 1000, 0.0)


def process_frame(message_id: bytes, data: dict) -> None:
    """Обрабатывает одно сообщение из стрима кадров."""
    started = time.monotonic()
    wait = _queue_wait_s(message_id)
    if wait is not None:
        stats.observe("queue_wait", wait)

    camera_id_raw = data.get(b"camera_id")
    if camera_id_raw is None:
        log.warning("Frame without camera_id, skipping")
        return
    camera_id = int(camera_id_raw.decode())

    camera = camera_cache.get_or_reload(camera_id)
    if camera is None:
        log.warning(f"Unknown camera_id={camera_id}, skipping frame")
        return

    with stats.measure("decode"):
        frame = _decode_frame(data)
    with stats.measure("model"):
        faces = extract_faces(frame)

    outcomes = Counter(_count_face(camera, f) for f in faces if f.embedding is not None)

    stats.count("frames")
    stats.count("faces", len(faces))
    stats.observe("frame_total", time.monotonic() - started)

    log.info(
        f"Cam {camera_id} ({camera.direction}) | faces={len(faces)} "
        f"| counted={outcomes[COUNTED]} | cooldown_skipped={outcomes[SKIPPED]}"
    )
