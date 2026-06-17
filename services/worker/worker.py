import os
import redis
import cv2
import numpy as np
import logging
import time
import threading
from dataclasses import dataclass

from face_engine import extract_faces
from face_storage_pg import FaceStoragePG
from db.stats_service import log_crossing
from db.database import wait_for_db, wait_for_redis, SessionLocal
from db.models import Camera

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("worker")

wait_for_db()
wait_for_redis()

BLOCK_MS = 2000   # ждём ответа 2 сек
SOCK_TIMEOUT = 5  # сокет живёт 5 сек — всегда больше BLOCK_MS/1000

# Сколько секунд не считаем повторно одного и того же человека на одной
# и той же камере. Без этого человек, который несколько секунд находится
# в кадре, даст десятки кадров с лицом -> десятки "пересечений" вместо одного.
# Не путать с трекингом траектории: это просто анти-дублирующий cooldown.
CROSSING_COOLDOWN_SECONDS = int(os.environ.get("CROSSING_COOLDOWN_SECONDS", "60"))

CAMERA_RELOAD_INTERVAL = 30  # сек, как у producer

r = redis.Redis(
    host="redis",
    port=6379,
    decode_responses=False,
    socket_timeout=SOCK_TIMEOUT,
    socket_connect_timeout=5,
    retry_on_timeout=True,
)

STREAM = "frames"

storage = FaceStoragePG()

last_id = "0"


@dataclass(frozen=True)
class CameraInfo:
    id: int
    direction: str   # 'in' | 'out'
    gate: str | None


# ── Кэш камер (id -> CameraInfo) с периодическим обновлением ────────────────
# Нужен, чтобы на каждый кадр не дёргать БД за direction/gate камеры.
# Сохраняем plain-объекты (не ORM), чтобы не зависеть от жизненного цикла
# закрытой сессии. Несколько реплик воркера (см. docker-compose: replicas: 2)
# читают общий Redis Stream, поэтому cooldown-ключи кладём в Redis, а не в
# память процесса.

_camera_cache: dict[int, CameraInfo] = {}
_camera_cache_lock = threading.Lock()


def _reload_cameras():
    session = SessionLocal()
    try:
        rows = session.query(Camera).all()
        snapshot = {c.id: CameraInfo(id=c.id, direction=c.direction, gate=c.gate) for c in rows}
        with _camera_cache_lock:
            _camera_cache.clear()
            _camera_cache.update(snapshot)
    except Exception as e:
        log.warning(f"Failed to reload cameras: {e}")
    finally:
        session.close()


def get_camera(camera_id: int) -> CameraInfo | None:
    with _camera_cache_lock:
        return _camera_cache.get(camera_id)


def _camera_reload_loop():
    while True:
        _reload_cameras()
        time.sleep(CAMERA_RELOAD_INTERVAL)


_reload_cameras()  # первичная загрузка перед стартом основного цикла
threading.Thread(target=_camera_reload_loop, daemon=True).start()


def _already_counted_recently(camera_id: int, person_id: int) -> bool:
    """True, если этого человека на этой камере уже считали в течение
    CROSSING_COOLDOWN_SECONDS. Атомарно ставит метку, если её ещё не было."""
    key = f"crossing_cooldown:{camera_id}:{person_id}"
    # NX=True -> ключ ставится только если его ещё нет; вернёт None, если уже был
    was_set = r.set(key, "1", ex=CROSSING_COOLDOWN_SECONDS, nx=True)
    return not was_set


while True:
    try:
        messages = r.xread(
            {STREAM: last_id},
            block=BLOCK_MS,
            count=1,
        )
    except redis.exceptions.TimeoutError:
        # block-период истёк раньше socket_timeout — нормальная ситуация
        continue
    except redis.exceptions.ConnectionError as e:
        log.warning(f"Redis connection lost: {e}, reconnecting in 2s...")
        time.sleep(2)
        continue

    if not messages:
        continue

    _, entries = messages[0]

    for message_id, data in entries:

        last_id = message_id.decode()

        camera_id_raw = data.get(b"camera_id")
        if camera_id_raw is None:
            log.warning("Frame without camera_id, skipping")
            continue
        camera_id = int(camera_id_raw.decode())

        camera = get_camera(camera_id)
        if camera is None:
            log.warning(f"Unknown camera_id={camera_id} (not in cache), skipping frame")
            continue

        frame = cv2.imdecode(
            np.frombuffer(data[b"frame"], np.uint8),
            cv2.IMREAD_COLOR
        )

        faces = extract_faces(frame)

        counted = 0
        skipped_cooldown = 0

        for face in faces:

            if face.embedding is None:
                continue

            res = storage.identify(face.embedding, face)
            person_id = res["id"]

            if _already_counted_recently(camera_id, person_id):
                skipped_cooldown += 1
                continue

            # Возраст/пол фиксируем по ТЕКУЩЕМУ кадру (в момент пересечения),
            # а не по усреднённому значению, сохранённому в Person.
            age = int(face.age) if face.age is not None else None
            gender = int(face.gender) if face.gender is not None else None

            log_crossing(camera, person_id, age, gender)
            counted += 1

        log.info(
            f"Cam {camera_id} ({camera.direction}) | faces={len(faces)} "
            f"| counted={counted} | cooldown_skipped={skipped_cooldown}"
        )
