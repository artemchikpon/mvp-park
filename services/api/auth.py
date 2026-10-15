"""
Авторизация API: только JWT (Authorization: Bearer <access-токен>).

Токен выдаёт POST /auth/login по логину и паролю пользователя из таблицы
users (см. routers/auth.py). Роли:
  admin  — всё: камеры, настройки, сотрудники, пользователи;
  viewer — только чтение (GET): статистика, списки, настройки.

Роль и активность пользователя читаются из БД на каждом запросе, поэтому
отключение пользователя или смена роли действуют сразу, а не после
истечения токена. Без JWT_SECRET сервис не стартует (см. security.py).

В Swagger (/docs) — кнопка Authorize: логин и пароль (OAuth2 password).
"""

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from api import security
from api.deps import get_db
from db.models import User

ADMIN, VIEWER = "admin", "viewer"
READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}

# auto_error=False: отсутствие токена обрабатываем сами — с понятным текстом.
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="auth/login",
    auto_error=False,
    description="Логин и пароль пользователя (таблица users)",
)


@dataclass(frozen=True)
class Principal:
    """Кто выполняет запрос."""
    user_id: int
    username: str
    role: str


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
    token: str | None = Security(oauth2_scheme),
    db: Session = Depends(get_db),
) -> Principal:
    if token is None:
        raise _unauthorized("Требуется авторизация: Authorization: Bearer <токен> (POST /auth/login)")
    try:
        user_id = security.decode_token(token, security.ACCESS)
    except security.InvalidToken:
        raise _unauthorized("Токен недействителен или истёк — войдите заново или обновите токен")
    user = active_user(db, user_id)
    if user is None:
        raise _unauthorized("Пользователь не найден или отключён")
    return Principal(user_id=user.id, username=user.username, role=user.role)


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
