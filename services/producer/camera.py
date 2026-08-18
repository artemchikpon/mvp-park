import cv2
import redis
import time
import logging
import threading
import os

from db.database import SessionLocal, wait_for_db, wait_for_redis
from db.models import Camera

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("producer")

wait_for_db()
wait_for_redis()

r = redis.Redis(
    host=os.environ.get("REDIS_HOST", "redis"),
    port=int(os.environ.get("REDIS_PORT", "6379")),
    decode_responses=False,
    socket_timeout=5,
    socket_connect_timeout=5,
    retry_on_timeout=True,
)
STREAM = "frames"
RELOAD_INTERVAL = 30


def stream_camera(camera_id: int, url: str, stop_event: threading.Event):
    log.info(f"[cam {camera_id}] Connecting: {url}")
    cap = cv2.VideoCapture(url)

    while not stop_event.is_set():
        ok, frame = cap.read()
        if not ok:
            log.warning(f"[cam {camera_id}] Read failed, reconnecting in 3s...")
            cap.release()
            time.sleep(3)
            cap = cv2.VideoCapture(url)
            continue

        ok, buf = cv2.imencode(".jpg", frame)
        if not ok:
            log.warning(f"[cam {camera_id}] JPEG encode failed")
            continue

        try:
            r.xadd(
                STREAM,
                {"frame": buf.tobytes(), "camera_id": str(camera_id)},
                maxlen=1000,
            )
        except redis.exceptions.RedisError as e:
            # Redis может временно исчезнуть. Поток камеры не должен умереть:
            # redis-py переподключится на следующей операции.
            log.warning(f"[cam {camera_id}] Redis unavailable: {e}; retrying in 2s")
            time.sleep(2)

    cap.release()
    log.info(f"[cam {camera_id}] Stopped.")


def get_active_cameras() -> dict[int, str]:
    """Возвращает {id: url} активных камер из БД."""
    session = SessionLocal()
    try:
        rows = session.query(Camera).filter(Camera.active == True).all()
        return {c.id: c.url for c in rows}
    finally:
        session.close()


def main():
    threads: dict[int, tuple[threading.Thread, threading.Event, str]] = {}

    while True:
        active = get_active_cameras()

        # Запустить новые камеры
        for cam_id, url in active.items():
            if cam_id not in threads:
                stop = threading.Event()
                t = threading.Thread(
                    target=stream_camera,
                    args=(cam_id, url, stop),
                    daemon=True
                )
                t.start()
                threads[cam_id] = (t, stop, url)
                log.info(f"Started camera {cam_id}")

        # Остановить удалённые / деактивированные
        for cam_id in list(threads.keys()):
            t, stop, old_url = threads[cam_id]
            if cam_id not in active:
                threads.pop(cam_id)
                stop.set()
                log.info(f"Stopped camera {cam_id}")
                continue

            # Перезапускаем умерший поток (например, после необработанного
            # исключения сторонней библиотеки) и применяем изменившийся URL.
            if not t.is_alive() or active[cam_id] != old_url:
                threads.pop(cam_id)
                stop.set()
                log.warning(f"Restarting camera {cam_id}: thread_alive={t.is_alive()}, url_changed={active[cam_id] != old_url}")
                new_stop = threading.Event()
                new_thread = threading.Thread(
                    target=stream_camera,
                    args=(cam_id, active[cam_id], new_stop),
                    daemon=True,
                )
                new_thread.start()
                threads[cam_id] = (new_thread, new_stop, active[cam_id])

        time.sleep(RELOAD_INTERVAL)


if __name__ == "__main__":
    main()
