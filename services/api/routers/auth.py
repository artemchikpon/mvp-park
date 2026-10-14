from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from api import security
from api.auth import Principal, active_user, get_principal
from api.deps import get_db
from api.schemas import MeOut, RefreshIn, TokenPair
from db.models import User

router = APIRouter(prefix="/auth", tags=["auth"])

_BAD_LOGIN = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Неверный логин или пароль",
    headers={"WWW-Authenticate": "Bearer"},
)


def _token_pair(user_id: int) -> TokenPair:
    return TokenPair(
        access_token=security.create_access_token(user_id),
        refresh_token=security.create_refresh_token(user_id),
        expires_in=security.ACCESS_TOKEN_MINUTES * 60,
    )


@router.post("/login", response_model=TokenPair, summary="Вход: логин и пароль -> access и refresh токены")
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # Форма OAuth2 (username/password) — её же отправляет кнопка Authorize в Swagger.
    user = db.query(User).filter(User.username == form.username.strip()).first()
    # Одинаковый ответ для "нет такого", "неверный пароль" и "отключён":
    # не подсказываем, какие логины существуют.
    if user is None or not user.is_active or not security.verify_password(form.password, user.password_hash):
        raise _BAD_LOGIN
    return _token_pair(user.id)


@router.post("/refresh", response_model=TokenPair, summary="Новая пара токенов по refresh-токену")
def refresh(body: RefreshIn, db: Session = Depends(get_db)):
    try:
        user_id = security.decode_token(body.refresh_token, security.REFRESH)
    except security.InvalidToken:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh-токен недействителен или истёк — войдите заново",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if active_user(db, user_id) is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Пользователь не найден или отключён",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _token_pair(user_id)


@router.get("/me", response_model=MeOut, summary="Кто я: роль и пользователь текущего запроса")
def me(principal: Principal = Depends(get_principal)):
    return MeOut(role=principal.role, user_id=principal.user_id, username=principal.username)
