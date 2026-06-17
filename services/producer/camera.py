import cv2
import redis
import time
import logging
import threading

from db.database import SessionLocal, wait_for_db, wait_for_redis
from db.models import Camera

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("producer")

wait_for_db()
wait_for_redis()

r = redis.Redis(host="redis", port=6379, decode_responses=False)
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

        _, buf = cv2.imencode(".jpg", frame)
        r.xadd(
            STREAM,
            {"frame": buf.tobytes(), "camera_id": str(camera_id)},
            maxlen=1000
        )

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
    threads: dict[int, tuple[threading.Thread, threading.Event]] = {}

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
                threads[cam_id] = (t, stop)
                log.info(f"Started camera {cam_id}")

        # Остановить удалённые / деактивированные
        for cam_id in list(threads.keys()):
            if cam_id not in active:
                _, stop = threads.pop(cam_id)
                stop.set()
                log.info(f"Stopped camera {cam_id}")

        time.sleep(RELOAD_INTERVAL)


if __name__ == "__main__":
    main()
