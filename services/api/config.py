"""Настройки api-сервиса из окружения.

API_KEYS читается в auth.py: без ключей сервис намеренно не стартует.
"""

from core.env import env_int, env_list, env_str

# Документация включается только строкой "true" (без учёта регистра).
ENABLE_DOCS = env_str("ENABLE_DOCS", "false").lower() == "true"

# CORS для отдельного dashboard/frontend. В production задайте CORS_ORIGINS,
# например: https://dashboard.example.com,https://admin.example.com
CORS_ORIGINS = env_list("CORS_ORIGINS", "*")

# Сколько api ждёт ответа воркера на задачу по сотрудникам.
EMPLOYEE_JOB_TIMEOUT = env_int("EMPLOYEE_SYNC_TIMEOUT", 120)
