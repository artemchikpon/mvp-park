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

# ── Ежедневный сброс галереи persons ─────────────────────────────────────────
PERSONS_RESET_HOUR = env_int("PERSONS_RESET_HOUR", 12)
PERSONS_RESET_MINUTE = env_int("PERSONS_RESET_MINUTE", 0)
PERSONS_RESET_LOCK_TTL = 300  # сек — с запасом на выполнение DELETE

# ── Сотрудники / HR API ──────────────────────────────────────────────────────
HR_API_BASE = env_str("HR_API_BASE", "https://192.168.0.146:4050").rstrip("/")
HR_SSL_VERIFY = env_bool("HR_SSL_VERIFY", True)
HR_EMPLOYEES_PAGE_LIMIT = env_int("HR_EMPLOYEES_PAGE_LIMIT", 100)
EMPLOYEE_JOB_RESULT_TTL = 300  # сколько живёт ответ на задачу от api
