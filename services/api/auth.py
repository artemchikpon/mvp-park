"""
Простая API-key авторизация для всех эндпоинтов.

Ключ передаётся в заголовке X-API-Key. Сравнение через secrets.compare_digest,
чтобы не течь через timing-атаку. Ключ(и) задаются переменной окружения
API_KEYS (через запятую, можно несколько — например, для разных клиентов
дашборда / интеграций).

Если API_KEYS не задана — сервис не стартует с "открытым" API по умолчанию,
а падает с понятной ошибкой (fail secure, а не fail open).

Заголовок объявлен через APIKeyHeader: так он попадает в OpenAPI как схема
безопасности, и в Swagger (/docs) появляется кнопка Authorize — ключ
вводится один раз для всех запросов.
"""

import secrets

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from core.env import env_list

API_KEYS = set(env_list("API_KEYS"))

if not API_KEYS:
    raise RuntimeError(
        "API_KEYS не задана. Укажите хотя бы один ключ через переменную "
        "окружения API_KEYS (можно несколько через запятую), например:\n"
        "  API_KEYS=change-me-please-1234567890"
    )


# auto_error=False: отсутствие заголовка обрабатываем сами, чтобы ответ был
# прежним (401 с понятным текстом), а не 403 по умолчанию у FastAPI.
api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="Ключ из переменной окружения API_KEYS",
)


def require_api_key(x_api_key: str | None = Security(api_key_header)) -> None:
    if x_api_key is None or not any(
        secrets.compare_digest(x_api_key, key) for key in API_KEYS
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный или отсутствующий API-ключ (заголовок X-API-Key)",
        )
