"""producer: читает RTSP-потоки активных камер и кладёт JPEG-кадры в Redis Stream.

Список камер перечитывается из БД каждые RELOAD_INTERVAL секунд: новые
камеры запускаются, выключенные/удалённые останавливаются, упавшие потоки и
камеры с изменённым URL перезапускаются.

С каждой камеры в стрим уходит не больше MAX_FPS кадров в секунду
(env PRODUCER_MAX_FPS, по умолчанию 5). Остальные кадры всё равно
вычитываются из потока (cap.grab), но не превращаются в изображение, не
сжимаются в JPEG и не отправляются. Читать их нужно обязательно: иначе они
копились бы в буфере и картинка всё сильнее отставала бы от реальности.
"""

import logging
import threading
import time
from dataclasses import dataclass

import cv2
import redis

from core import perf
from core.redis_keys import FRAMES_STREAM
from core.env import env_float, env_int, env_str
from core.redis_utils import wait_for_redis
from db.database import SessionLocal, wait_for_db
from db.models import Camera

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("producer")

RELOAD_INTERVAL = 30       # сек между перечитываниями списка камер
STREAM_MAXLEN = 1000       # сколько последних кадров хранит стрим
RECONNECT_DELAY_S = 3      # пауза перед переподключением к камере
REDIS_RETRY_DELAY_S = 2    # пауза, если Redis временно недоступен

# Максимум кадров в секунду с одной камеры в Redis. 0 — без ограничения.
MAX_FPS = env_float("PRODUCER_MAX_FPS", 5.0)

# Сводка раз в PERF_LOG_INTERVAL с: по каждой камере сколько кадров прочитано
# (grabbed) и отправлено (sent), сколько стоят retrieve/JPEG/XADD, и сколько
# ядер CPU занимает процесс (в т.ч. распаковка видео внутри grab()).
stats = perf.get("producer")

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


class FrameRateLimiter:
    """Пропускает не больше max_fps событий в секунду.

    Работает по расписанию: следующий кадр "положен" через 1/max_fps после
    предыдущего слота, поэтому средняя частота держится ровно на max_fps,
    даже если кадры камеры приходят неравномерно. Если поток надолго
    отстал (пауза, переподключение), расписание сбрасывается — без
    "догоняющей" пачки кадров.
    """

    def __init__(self, max_fps: float, clock=time.monotonic):
        self.interval = 1.0 / max_fps if max_fps > 0 else 0.0
        self._clock = clock
        self._next_at: float | None = None

    def ready(self) -> bool:
        if self.interval == 0.0:
            return True
        now = self._clock()
        if self._next_at is None or now >= self._next_at + self.interval:
            self._next_at = now + self.interval
            return True
        if now >= self._next_at:
            self._next_at += self.interval
            return True
        return False


@dataclass
class CameraWorker:
    thread: threading.Thread
    stop: threading.Event
    url: str


def stream_camera(camera_id: int, url: str, stop_event: threading.Event) -> None:
    log.info(f"[cam {camera_id}] Connecting: {url} (max {MAX_FPS:g} fps, 0 = no limit)")
    cap = cv2.VideoCapture(url)
    limiter = FrameRateLimiter(MAX_FPS)

    while not stop_event.is_set():
        # grab() забирает очередной кадр из потока; делаем это для каждого
        # кадра, чтобы не копилась задержка. Дорогие шаги — retrieve()
        # (перевод в BGR-изображение), JPEG и XADD — только для кадров,
        # прошедших ограничение частоты.
        ok = cap.grab()
        if ok:
            stats.count(f"cam{camera_id}.grabbed")
            if not limiter.ready():
                continue
            with stats.measure("retrieve"):
                ok, frame = cap.retrieve()
        if not ok:
            stats.count(f"cam{camera_id}.read_failed")
            log.warning(f"[cam {camera_id}] Read failed, reconnecting in {RECONNECT_DELAY_S}s...")
            cap.release()
            time.sleep(RECONNECT_DELAY_S)
            cap = cv2.VideoCapture(url)
            continue

        with stats.measure("jpeg"):
            ok, buf = cv2.imencode(".jpg", frame)
        if not ok:
            log.warning(f"[cam {camera_id}] JPEG encode failed")
            continue

        try:
            with stats.measure("xadd"):
                r.xadd(
                    FRAMES_STREAM,
                    {"frame": buf.tobytes(), "camera_id": str(camera_id)},
                    maxlen=STREAM_MAXLEN,
                )
            stats.count(f"cam{camera_id}.sent")
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
    stats.start_reporter()

    workers: dict[int, CameraWorker] = {}
    while True:
        sync_workers(workers, get_active_cameras())
        time.sleep(RELOAD_INTERVAL)


if __name__ == "__main__":
    main()
