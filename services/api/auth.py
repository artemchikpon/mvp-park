"""
Простая API-key авторизация для всех эндпоинтов.

Ключ передаётся в заголовке X-API-Key. Сравнение через secrets.compare_digest,
чтобы не течь через timing-атаку. Ключ(и) задаются переменной окружения
API_KEYS (через запятую, можно несколько — например, для разных клиентов
дашборда / интеграций).

Если API_KEYS не задана — сервис не стартует с "открытым" API по умолчанию,
а падает с понятной ошибкой (fail secure, а не fail open).
"""

import os
import secrets
from fastapi import Header, HTTPException, status

_raw = os.environ.get("API_KEYS", "")
API_KEYS = {k.strip() for k in _raw.split(",") if k.strip()}

if not API_KEYS:
    raise RuntimeError(
        "API_KEYS не задана. Укажите хотя бы один ключ через переменную "
        "окружения API_KEYS (можно несколько через запятую), например:\n"
        "  API_KEYS=change-me-please-1234567890"
    )


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if x_api_key is None or not any(
        secrets.compare_digest(x_api_key, key) for key in API_KEYS
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный или отсутствующий API-ключ (заголовок X-API-Key)",
        )
