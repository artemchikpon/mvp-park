import json
import os
import socket
import uuid
import redis
import cv2
import numpy as np
import logging
import time
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from face_engine import extract_faces
from face_storage_pg import FaceStoragePG
from employee_sync import sync_employees, add_employee_from_file
from db.stats_service import log_crossing, get_settings, reset_persons
from db.database import wait_for_db, wait_for_redis, SessionLocal
from db.models import Camera

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("worker")

BLOCK_MS = 2000   # ждём ответа 2 сек
SOCK_TIMEOUT = 5  # сокет живёт 5 сек — всегда больше BLOCK_MS/1000

# Сколько секунд не считаем повторно одного и того же человека на одной
# и той же камере. Без этого человек, который несколько секунд находится
# в кадре, даст десятки кадров с лицом -> десятки "пересечений" вместо одного.
# Не путать с трекингом траектории: это просто анти-дублирующий cooldown.
CROSSING_COOLDOWN_SECONDS = int(os.environ.get("CROSSING_COOLDOWN_SECONDS", "60"))

CAMERA_RELOAD_INTERVAL = 30  # сек, как у producer

STREAM = "frames"

# ── Consumer group ───────────────────────────────────────────────────────────
# Раньше воркер читал стрим через обычный XREAD с локальным last_id: при
# нескольких репликах (см. docker-compose: replicas: 2) каждая реплика
# независимо вычитывала ВЕСЬ стрим — т.е. кадры не распределялись, а
# дублировались (двойная обработка одних и тех же кадров, впустую тратился
# CPU). Consumer group решает это: Redis сам раздаёт каждое сообщение ровно
# одному консьюмеру группы.
GROUP = "workers"
# Уникальное имя консьюмера на реплику: hostname (в Docker Swarm/Compose это
# id контейнера) + короткий uuid — на случай гонки при одновременном рестарте
# нескольких реплик с одинаковым hostname.
CONSUMER = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"

# NB: конструктор redis.Redis(...) ленивый — реального сетевого соединения
# при создании объекта не происходит, поэтому создавать клиент на уровне
# модуля безопасно и не мешает импорту модуля в тестах.
r = redis.Redis(
    host=os.environ.get("REDIS_HOST", "redis"),
    port=int(os.environ.get("REDIS_PORT", "6379")),
    decode_responses=False,
    socket_timeout=SOCK_TIMEOUT,
    socket_connect_timeout=5,
    retry_on_timeout=True,
)

# FaceStoragePG() ничего не подключает в конструкторе (сессии открываются
# лениво внутри identify()/is_employee()), поэтому тоже безопасно на
# уровне модуля.
storage = FaceStoragePG()


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


# ── Ежедневный сброс таблицы persons ─────────────────────────────────────────
# Продуктовое требование: галерея векторов уникальных лиц (persons) должна
# каждый день начинаться "с нуля" — сбрасываем её раз в сутки в 12:00 по
# таймзоне парка (park_settings.timezone, та же настройка, что и для
# Live Occupancy/дашборда). Время сброса можно переопределить через env
# (например, для отладки).
PERSONS_RESET_HOUR = int(os.environ.get("PERSONS_RESET_HOUR", "12"))
PERSONS_RESET_MINUTE = int(os.environ.get("PERSONS_RESET_MINUTE", "0"))

# Реплик воркера несколько (см. docker-compose: replicas: 2), и все они
# просыпаются в одну и ту же минуту. Redis-лок с TTL и SET NX гарантирует,
# что реально удалит записи только одна реплика — тот же паттерн, что и у
# cooldown-резервации выше.
PERSONS_RESET_LOCK_KEY = "persons_reset_lock"
PERSONS_RESET_LOCK_TTL = 300  # сек — с запасом на выполнение DELETE


def _park_timezone() -> ZoneInfo:
    """Таймзона парка из park_settings. При любой ошибке — UTC, чтобы поток
    сброса не падал и не блокировал остальную работу воркера."""
    session = SessionLocal()
    try:
        return ZoneInfo(get_settings(session).timezone)
    except Exception as e:
        log.warning(f"[persons_reset] Не удалось прочитать таймзону парка, использую UTC: {e}")
        return ZoneInfo("UTC")
    finally:
        session.close()


def _seconds_until_next_persons_reset() -> float:
    tz = _park_timezone()
    now = datetime.now(tz)
    target = now.replace(
        hour=PERSONS_RESET_HOUR, minute=PERSONS_RESET_MINUTE, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _run_persons_reset_once() -> None:
    tz = _park_timezone()
    today = datetime.now(tz).date().isoformat()
    lock_key = f"{PERSONS_RESET_LOCK_KEY}:{today}"

    # nx=True: если лок на сегодня уже стоит — сброс уже выполнила (или
    # выполняет) другая реплика, повторно ничего не делаем.
    if not r.set(lock_key, CONSUMER, ex=PERSONS_RESET_LOCK_TTL, nx=True):
        log.info("[persons_reset] Сброс persons на сегодня уже выполнен другой репликой")
        return

    session = SessionLocal()
    try:
        deleted = reset_persons(session)
        # Локальный in-memory кэш векторов (см. FaceStoragePG) может ещё
        # какое-то время (до CACHE_TTL_SECONDS) отдавать удалённые записи —
        # инвалидируем сразу, чтобы новые лица не "склеивались" со старыми.
        FaceStoragePG.invalidate_cache()
        log.info(
            f"[persons_reset] Таблица persons очищена: удалено {deleted} записей "
            f"(плановый сброс {PERSONS_RESET_HOUR:02d}:{PERSONS_RESET_MINUTE:02d} "
            f"по таймзоне парка)"
        )
    except Exception:
        log.exception("[persons_reset] Ошибка сброса таблицы persons")
        # Снимаем лок, чтобы попытку можно было безопасно повторить (иначе
        # до конца дня persons так и останется незачищенной).
        try:
            r.delete(lock_key)
        except redis.exceptions.RedisError:
            log.warning("[persons_reset] Не удалось снять лок после ошибки")
    finally:
        session.close()


def _persons_reset_loop():
    while True:
        try:
            time.sleep(_seconds_until_next_persons_reset())
            _run_persons_reset_once()
        except Exception:
            log.exception("[persons_reset] Ошибка в цикле сброса; повтор через 60с")
            time.sleep(60)


EMPLOYEE_SYNC_JOBS_KEY = "employee_sync_jobs"
EMPLOYEE_ADD_JOBS_KEY = "employee_add_jobs"
EMPLOYEE_SYNC_RESULT_TTL = 300


def _employee_sync_job_listener():
    while True:
        try:
            job = r.blpop([EMPLOYEE_SYNC_JOBS_KEY, EMPLOYEE_ADD_JOBS_KEY], timeout=5)
        except redis.exceptions.ConnectionError as e:
            log.warning(f"[employee_listener] Redis connection lost: {e}, retrying in 2s...")
            time.sleep(2)
            continue

        if job is None:
            continue

        queue_raw, payload_raw = job
        queue = queue_raw.decode()
        payload = payload_raw.decode()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            # Совместимость с задачами, поставленными старой версией API.
            data = {"job_id": payload}
        job_id = data["job_id"]

        if queue == EMPLOYEE_SYNC_JOBS_KEY:
            log.info(f"[employee_listener] Получена задача полной синхронизации job_id={job_id}")
            try:
                result = sync_employees()
            except Exception as e:
                log.error(f"[employee_listener] Ошибка синхронизации job_id={job_id}: {e}")
                result = {"error": str(e)}
            result_key = f"employee_sync_result:{job_id}"
        else:
            log.info(f"[employee_listener] Получена задача добавления сотрудника job_id={job_id}")
            try:
                result = add_employee_from_file(
                    first_name=data["first_name"],
                    file_id=int(data["file_id"]),
                    external_id=data.get("external_id"),
                )
            except Exception as e:
                log.error(f"[employee_listener] Ошибка добавления job_id={job_id}: {e}")
                result = {"error": str(e)}
            result_key = f"employee_add_result:{job_id}"
        try:
            r.rpush(result_key, json.dumps(result))
            r.expire(result_key, EMPLOYEE_SYNC_RESULT_TTL)
        except redis.exceptions.ConnectionError as e:
            log.error(f"[employee_sync_listener] Не удалось записать результат job_id={job_id}: {e}")


def _cooldown_key(camera_id: int, person_id: int) -> str:
    return f"crossing_cooldown:{camera_id}:{person_id}"


def _already_counted_recently(camera_id: int, person_id: int, reserve: bool = True) -> bool:
    """Проверяет cooldown.

    Для обратной совместимости прямые вызовы с reserve=True ставят метку.
    Рабочий путь _process_entry использует reserve=False и резервирует метку
    отдельно, снимая её при ошибке БД.
    """
    if r.exists(_cooldown_key(camera_id, person_id)):
        return True
    if reserve:
        return _reserve_cooldown(camera_id, person_id) is None
    return False


def _reserve_cooldown(camera_id: int, person_id: int) -> str | None:
    """Атомарно резервирует cooldown и возвращает token владельца."""
    token = uuid.uuid4().hex
    if r.set(_cooldown_key(camera_id, person_id), token,
             ex=CROSSING_COOLDOWN_SECONDS, nx=True):
        return token
    return None


def _release_cooldown(camera_id: int, person_id: int, token: str) -> None:
    """Удаляет cooldown только если ключ всё ещё принадлежит этой попытке."""
    key = _cooldown_key(camera_id, person_id)
    script = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
        return redis.call('del', KEYS[1])
    end
    return 0
    """
    try:
        r.eval(script, 1, key, token)
    except redis.exceptions.RedisError:
        log.warning("Не удалось освободить cooldown %s", key)


def _process_entry(message_id: bytes, data: dict) -> None:
    camera_id_raw = data.get(b"camera_id")
    if camera_id_raw is None:
        log.warning("Frame without camera_id, skipping")
        return
    camera_id = int(camera_id_raw.decode())

    camera = get_camera(camera_id)
    if camera is None:
        # Не ждём следующего 30-секундного reload: новая камера могла появиться
        # сразу после последнего обновления кэша.
        _reload_cameras()
        camera = get_camera(camera_id)
        if camera is None:
            log.warning(f"Unknown camera_id={camera_id}, skipping frame")
            return

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

        # None означает что лицо опознано как сотрудник — игнорируем
        if res is None:
            log.debug("Лицо сотрудника — пропускаем")
            skipped_cooldown += 1
            continue

        person_id = res["id"]

        if _already_counted_recently(camera_id, person_id, reserve=False):
            skipped_cooldown += 1
            continue

        token = _reserve_cooldown(camera_id, person_id)
        if token is None:
            skipped_cooldown += 1
            continue

        # Возраст/пол фиксируем по ТЕКУЩЕМУ кадру (в момент пересечения),
        # а не по усреднённому значению, сохранённому в Person.
        age = int(face.age) if face.age is not None else None
        gender = int(face.gender) if face.gender is not None else None

        if log_crossing(camera, person_id, age, gender):
            counted += 1
        else:
            # БД не приняла событие: cooldown должен быть снят, чтобы
            # следующий кадр мог повторить запись.
            _release_cooldown(camera_id, person_id, token)

    log.info(
        f"Cam {camera_id} ({camera.direction}) | faces={len(faces)} "
        f"| counted={counted} | cooldown_skipped={skipped_cooldown}"
    )


# Сколько раз реплика-держатель сообщения должна не отвечать (по времени
# простоя ownership), прежде чем считаем её мёртвой и забираем сообщение
# себе. Защищает от "потерянных" кадров, если реплика упала посреди обработки
# (без XACK сообщение так и осталось бы висеть в PEL этой реплики навсегда).
CLAIM_IDLE_MS = 30_000
CLAIM_INTERVAL_S = 15


def _ensure_consumer_group():
    try:
        r.xgroup_create(STREAM, GROUP, id="$", mkstream=True)
        log.info(f"Создана consumer group '{GROUP}' на стриме '{STREAM}'")
    except redis.exceptions.ResponseError as e:
        if "BUSYGROUP" in str(e):
            log.info(f"Consumer group '{GROUP}' уже существует")
        else:
            raise


def _main_loop():
    last_claim_check = 0.0

    while True:
        try:
            # ">" значит "только новые, ещё никому не выданные сообщения"
            messages = r.xreadgroup(
                GROUP, CONSUMER,
                {STREAM: ">"},
                block=BLOCK_MS,
                count=1,
            )
        except redis.exceptions.TimeoutError:
            messages = None
        except redis.exceptions.ConnectionError as e:
            log.warning(f"Redis connection lost: {e}, reconnecting in 2s...")
            time.sleep(2)
            continue

        if messages:
            _, entries = messages[0]
            for message_id, data in entries:
                try:
                    _process_entry(message_id, data)
                    # ACK только после успешной обработки. При исключении
                    # сообщение остаётся в PEL и будет подобрано XAUTOCLAIM.
                    r.xack(STREAM, GROUP, message_id)
                except Exception:
                    log.exception(f"Ошибка обработки сообщения {message_id}; "
                                  f"сообщение останется в PEL для повторной обработки")

        # Периодически подбираем сообщения от "зависших"/упавших реплик
        now = time.time()
        if now - last_claim_check > CLAIM_INTERVAL_S:
            last_claim_check = now
            try:
                claim_cursor = b"0-0"
                while True:
                    next_cursor, claimed, _ = r.xautoclaim(
                        STREAM, GROUP, CONSUMER,
                        min_idle_time=CLAIM_IDLE_MS,
                        start_id=claim_cursor,
                        count=10,
                    )
                    for message_id, data in claimed:
                        try:
                            _process_entry(message_id, data)
                            r.xack(STREAM, GROUP, message_id)
                        except Exception:
                            log.exception(f"Ошибка повторной обработки {message_id}; "
                                          f"сообщение останется в PEL")
                    if not claimed or next_cursor in (b"0-0", "0-0"):
                        break
                    claim_cursor = next_cursor
            except redis.exceptions.ConnectionError as e:
                log.warning(f"Redis connection lost during XAUTOCLAIM: {e}")


def main():
    wait_for_db()
    wait_for_redis()
    _ensure_consumer_group()

    _reload_cameras()  # первичная загрузка перед стартом основного цикла
    threading.Thread(target=_camera_reload_loop, daemon=True).start()
    threading.Thread(target=_persons_reset_loop, daemon=True).start()

    # Не блокируем обработку кадров из-за медленного HR API. Синхронизация
    # выполняется в отдельном фоне, а очередь /employees/sync продолжает
    # работать через отдельный listener.
    threading.Thread(
        target=lambda: sync_employees(),
        name="employee-initial-sync",
        daemon=True,
    ).start()
    threading.Thread(target=_employee_sync_job_listener, daemon=True).start()

    _main_loop()


if __name__ == "__main__":
    main()
