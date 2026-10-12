"""producer: читает RTSP-потоки активных камер и кладёт JPEG-кадры в Redis Stream.

Список камер перечитывается из БД каждые RELOAD_INTERVAL секунд: новые
камеры запускаются, выключенные/удалённые останавливаются, упавшие потоки и
камеры с изменённым URL перезапускаются.
"""

import logging
import threading
import time
from dataclasses import dataclass

import cv2
import redis

from core.redis_keys import FRAMES_STREAM
from core.env import env_int, env_str
from core.redis_utils import wait_for_redis
from db.database import SessionLocal, wait_for_db
from db.models import Camera

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("producer")

RELOAD_INTERVAL = 30       # сек между перечитываниями списка камер
STREAM_MAXLEN = 1000       # сколько последних кадров хранит стрим
RECONNECT_DELAY_S = 3      # пауза перед переподключением к камере
REDIS_RETRY_DELAY_S = 2    # пауза, если Redis временно недоступен

# Клиент создаётся напрямую, а не через core.redis_utils.make_redis: так
# DeprecationWarning redis-py про retry_on_timeout (redis>=6) не всплывает в
# логах producer — поведение и вывод как в исходной версии.
r = redis.Redis(
    host=env_str("REDIS_HOST", "redis"),
    port=env_int("REDIS_PORT", 6379),
    decode_responses=False,
    socket_timeout=5,
    socket_connect_timeout=5,
    retry_on_timeout=True,
)


@dataclass
class CameraWorker:
    thread: threading.Thread
    stop: threading.Event
    url: str


def stream_camera(camera_id: int, url: str, stop_event: threading.Event) -> None:
    log.info(f"[cam {camera_id}] Connecting: {url}")
    cap = cv2.VideoCapture(url)

    while not stop_event.is_set():
        ok, frame = cap.read()
        if not ok:
            log.warning(f"[cam {camera_id}] Read failed, reconnecting in {RECONNECT_DELAY_S}s...")
            cap.release()
            time.sleep(RECONNECT_DELAY_S)
            cap = cv2.VideoCapture(url)
            continue

        ok, buf = cv2.imencode(".jpg", frame)
        if not ok:
            log.warning(f"[cam {camera_id}] JPEG encode failed")
            continue

        try:
            r.xadd(
                FRAMES_STREAM,
                {"frame": buf.tobytes(), "camera_id": str(camera_id)},
                maxlen=STREAM_MAXLEN,
            )
        except redis.exceptions.RedisError as e:
            # Redis может временно исчезнуть. Поток камеры не должен умереть:
            # redis-py переподключится на следующей операции.
            log.warning(f"[cam {camera_id}] Redis unavailable: {e}; retrying in {REDIS_RETRY_DELAY_S}s")
            time.sleep(REDIS_RETRY_DELAY_S)

    cap.release()
    log.info(f"[cam {camera_id}] Stopped.")


def get_active_cameras() -> dict[int, str]:
    """Возвращает {id: url} активных камер из БД."""
    session = SessionLocal()
    try:
        rows = session.query(Camera).filter(Camera.active == True).all()  # noqa: E712
        return {c.id: c.url for c in rows}
    finally:
        session.close()


def _start(camera_id: int, url: str) -> CameraWorker:
    stop = threading.Event()
    thread = threading.Thread(target=stream_camera, args=(camera_id, url, stop),
                              name=f"camera-{camera_id}", daemon=True)
    thread.start()
    return CameraWorker(thread=thread, stop=stop, url=url)


def sync_workers(workers: dict[int, CameraWorker], active: dict[int, str]) -> None:
    """Приводит набор запущенных потоков в соответствие со списком камер."""
    # Запустить новые камеры
    for cam_id, url in active.items():
        if cam_id not in workers:
            workers[cam_id] = _start(cam_id, url)
            log.info(f"Started camera {cam_id}")

    # Остановить удалённые / деактивированные
    for cam_id in list(workers):
        w = workers[cam_id]
        if cam_id not in active:
            workers.pop(cam_id).stop.set()
            log.info(f"Stopped camera {cam_id}")
            continue

        # Перезапускаем умерший поток (например, после необработанного
        # исключения сторонней библиотеки) и применяем изменившийся URL.
        alive, url_changed = w.thread.is_alive(), active[cam_id] != w.url
        if not alive or url_changed:
            w.stop.set()
            log.warning(f"Restarting camera {cam_id}: thread_alive={alive}, url_changed={url_changed}")
            workers[cam_id] = _start(cam_id, active[cam_id])


def main() -> None:
    wait_for_db()
    wait_for_redis()

    workers: dict[int, CameraWorker] = {}
    while True:
        sync_workers(workers, get_active_cameras())
        time.sleep(RELOAD_INTERVAL)


if __name__ == "__main__":
    main()
