"""Кэш камер (id -> CameraInfo) с периодическим обновлением из БД.

Нужен, чтобы на каждый кадр не дёргать БД за direction/gate камеры.
Храним plain-объекты (не ORM), чтобы не зависеть от жизненного цикла сессии.
"""

import logging
import threading
import time
from dataclasses import dataclass

from db.database import SessionLocal
from db.models import Camera
from config import CAMERA_RELOAD_INTERVAL

log = logging.getLogger("worker")


@dataclass(frozen=True)
class CameraInfo:
    id: int
    direction: str   # 'in' | 'out'
    gate: str | None


_cache: dict[int, CameraInfo] = {}
_lock = threading.Lock()


def reload_cameras() -> None:
    session = SessionLocal()
    try:
        snapshot = {
            c.id: CameraInfo(id=c.id, direction=c.direction, gate=c.gate)
            for c in session.query(Camera).all()
        }
        with _lock:
            _cache.clear()
            _cache.update(snapshot)
    except Exception as e:
        log.warning(f"Failed to reload cameras: {e}")
    finally:
        session.close()


def get_camera(camera_id: int) -> CameraInfo | None:
    with _lock:
        return _cache.get(camera_id)


def get_or_reload(camera_id: int) -> CameraInfo | None:
    """Если камеры нет в кэше — сразу перечитываем БД, не дожидаясь
    планового обновления: новая камера могла появиться только что."""
    camera = get_camera(camera_id)
    if camera is None:
        reload_cameras()
        camera = get_camera(camera_id)
    return camera


def reload_loop() -> None:
    while True:
        reload_cameras()
        time.sleep(CAMERA_RELOAD_INTERVAL)
