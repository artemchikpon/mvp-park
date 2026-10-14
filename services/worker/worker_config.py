"""Настройки worker-сервиса (все берутся из окружения один раз при импорте)."""

import socket
import uuid

from core.env import env_bool, env_int, env_str

# ── Чтение стрима кадров ─────────────────────────────────────────────────────
BLOCK_MS = 2000      # сколько ждём новых сообщений в XREADGROUP
SOCK_TIMEOUT = 5     # таймаут сокета Redis — всегда больше BLOCK_MS/1000

# Сообщение, которое реплика-держатель не подтвердила (XACK) дольше
# CLAIM_IDLE_MS, считается "потерянным" (реплика упала посреди обработки)
# и забирается другой репликой через XAUTOCLAIM.
CLAIM_IDLE_MS = 30_000
CLAIM_INTERVAL_S = 15

# Уникальное имя консьюмера на реплику: hostname (в Docker это id
# контейнера) + короткий uuid — на случай гонки при одновременном рестарте
# нескольких реплик с одинаковым hostname.
CONSUMER_NAME = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"

# ── Подсчёт пересечений ──────────────────────────────────────────────────────
# Сколько секунд не считаем повторно одного и того же человека на одной
# и той же камере. Без этого человек, который несколько секунд находится
# в кадре, даст десятки кадров с лицом -> десятки "пересечений" вместо одного.
# Не путать с трекингом траектории: это просто анти-дублирующий cooldown.
CROSSING_COOLDOWN_SECONDS = env_int("CROSSING_COOLDOWN_SECONDS", 60)

CAMERA_RELOAD_INTERVAL = 30  # сек, как у producer

# ── Устройство и роль воркера (разделение GPU / CPU) ────────────────────────
# FACE_DEVICE: cpu | gpu — на чём считать модель InsightFace (см. device.py).
FACE_DEVICE = env_str("FACE_DEVICE", "cpu").strip().lower()

# WORKER_ROLE:
#   primary  — берёт кадры из очереди всегда (поведение по умолчанию);
#   overflow — подстраховка: берёт новые кадры, только когда в очереди
#              накопилось не меньше OVERFLOW_MIN_BACKLOG невыданных кадров
#              (основные воркеры не успевают или GPU-воркер упал). Зависшие
#              у упавших реплик сообщения (XAUTOCLAIM) забирает всегда.
WORKER_ROLE = env_str("WORKER_ROLE", "primary").strip().lower()
OVERFLOW_MIN_BACKLOG = env_int("OVERFLOW_MIN_BACKLOG", 10)
OVERFLOW_IDLE_SLEEP_S = 0.5

# FACE_CPU_THREADS: сколько потоков процессора onnxruntime даёт одной модели
# (intra-op). 0 — сколько решит onnxruntime (по числу ядер), как раньше.
# Потоки в любом случае засыпают между кадрами (см. device.session_options).
FACE_CPU_THREADS = env_int("FACE_CPU_THREADS", 0)

if FACE_DEVICE not in ("cpu", "gpu"):
    raise ValueError(f"FACE_DEVICE должен быть cpu или gpu, получено: {FACE_DEVICE!r}")
if WORKER_ROLE not in ("primary", "overflow"):
    raise ValueError(f"WORKER_ROLE должен быть primary или overflow, получено: {WORKER_ROLE!r}")
if FACE_CPU_THREADS < 0:
    raise ValueError(f"FACE_CPU_THREADS должен быть >= 0, получено: {FACE_CPU_THREADS}")

# ── Ежедневный сброс галереи persons ─────────────────────────────────────────
PERSONS_RESET_HOUR = env_int("PERSONS_RESET_HOUR", 12)
PERSONS_RESET_MINUTE = env_int("PERSONS_RESET_MINUTE", 0)
PERSONS_RESET_LOCK_TTL = 300  # сек — с запасом на выполнение DELETE

# ── Сотрудники / HR API ──────────────────────────────────────────────────────
HR_API_BASE = env_str("HR_API_BASE", "https://192.168.0.146:4050").rstrip("/")
HR_SSL_VERIFY = env_bool("HR_SSL_VERIFY", True)
HR_EMPLOYEES_PAGE_LIMIT = env_int("HR_EMPLOYEES_PAGE_LIMIT", 100)
EMPLOYEE_JOB_RESULT_TTL = 300  # сколько живёт ответ на задачу от api
