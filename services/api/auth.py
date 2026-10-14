"""
Авторизация API: JWT для администрации и API-ключи для интеграций.

Два способа, проверяются на каждом запросе:

1. **JWT** (Authorization: Bearer <access-токен>) — люди из администрации.
   Токен выдаёт POST /auth/login по логину и паролю (таблица users).
   Роли:
     admin  — всё: камеры, настройки, сотрудники, пользователи;
     viewer — только чтение (GET): статистика, списки, настройки.
2. **API-ключ** (X-API-Key) — скрипты и интеграции (например, сервер
   дашборда). Ключи задаются в API_KEYS через запятую и дают права admin,
   как и раньше. API_KEYS можно не задавать — тогда вход только по JWT.

Сравнение ключей — secrets.compare_digest (без утечки через время ответа).
Без JWT_SECRET сервис не стартует (см. security.py) — fail secure.

В Swagger (/docs) кнопка Authorize предлагает оба способа: логин/пароль
(OAuth2 password) или ключ.
"""

import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer
from sqlalchemy.orm import Session

from api import security
from api.deps import get_db
from core.env import env_list
from db.models import User

API_KEYS = set(env_list("API_KEYS"))

ADMIN, VIEWER = "admin", "viewer"
READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}

# auto_error=False: отсутствие заголовков обрабатываем сами — один понятный
# 401 на оба способа входа (по умолчанию FastAPI вернул бы 401/403 сам).
api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="Ключ из переменной окружения API_KEYS (права admin)",
)
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="auth/login",
    auto_error=False,
    description="Логин и пароль пользователя (таблица users)",
)


@dataclass(frozen=True)
class Principal:
    """Кто выполняет запрос."""
    role: str
    user_id: int | None = None      # None — вход по API-ключу
    username: str | None = None


def api_key_is_valid(key: str | None) -> bool:
    return bool(key) and any(secrets.compare_digest(key, k) for k in API_KEYS)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def active_user(db: Session, user_id: int) -> User | None:
    user = db.get(User, user_id)
    return user if user is not None and user.is_active else None


def get_principal(
    api_key: str | None = Security(api_key_header),
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
) -> Principal:
    if api_key is not None:
        if api_key_is_valid(api_key):
            return Principal(role=ADMIN)
        raise _unauthorized("Неверный API-ключ (заголовок X-API-Key)")

    if token is not None:
        try:
            user_id = security.decode_token(token, security.ACCESS)
        except security.InvalidToken:
            raise _unauthorized("Токен недействителен или истёк — войдите заново или обновите токен")
        user = active_user(db, user_id)
        if user is None:
            raise _unauthorized("Пользователь не найден или отключён")
        return Principal(role=user.role, user_id=user.id, username=user.username)

    raise _unauthorized(
        "Требуется авторизация: Authorization: Bearer <токен> (POST /auth/login) "
        "или заголовок X-API-Key"
    )


def authorize(request: Request, principal: Principal = Depends(get_principal)) -> Principal:
    """Для бизнес-роутеров: viewer — только чтение, остальное — admin."""
    if request.method not in READ_ONLY_METHODS and principal.role != ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Недостаточно прав: изменять данные может только admin",
        )
    return principal


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if principal.role != ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Недостаточно прав: нужна роль admin",
        )
    return principal
