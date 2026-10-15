"""Пароли и JWT-токены администрации.

Пароли — scrypt из стандартной библиотеки (без внешних зависимостей), в БД
хранится строка "scrypt$n$r$p$соль$хэш". Сравнение — постоянное по времени.

Токены — JWT (HS256), подписаны JWT_SECRET:
  - access  (ACCESS_TOKEN_MINUTES, по умолчанию 1440 мин = 1 день) — для запросов к API;
  - refresh (REFRESH_TOKEN_DAYS, по умолчанию 14 дней) — только для
    POST /auth/refresh, чтобы получить новую пару без ввода пароля.
В токене лежит id пользователя (sub) и тип; роль и активность при каждом
запросе берутся из БД, поэтому отключение пользователя или смена роли
действуют сразу, а не после истечения токена.
"""

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from core.env import env_int, env_str

JWT_SECRET = env_str("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_MINUTES = env_int("ACCESS_TOKEN_MINUTES", 24 * 60)  # 1 день
REFRESH_TOKEN_DAYS = env_int("REFRESH_TOKEN_DAYS", 14)

ACCESS, REFRESH = "access", "refresh"

if len(JWT_SECRET) < 32:
    # Без секрета сервис не стартует (fail secure), а не работает с открытым API.
    # Короткий секрет HS256 подбирается перебором по любому выданному токену.
    raise RuntimeError(
        "JWT_SECRET не задан или короче 32 символов. Сгенерируйте, например:\n"
        "  JWT_SECRET=$(openssl rand -hex 32)"
    )


# ── пароли ───────────────────────────────────────────────────────────────────

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt,
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# ── токены ───────────────────────────────────────────────────────────────────

def _create_token(user_id: int, token_type: str, lifetime: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "type": token_type, "iat": now, "exp": now + lifetime}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_access_token(user_id: int) -> str:
    return _create_token(user_id, ACCESS, timedelta(minutes=ACCESS_TOKEN_MINUTES))


def create_refresh_token(user_id: int) -> str:
    return _create_token(user_id, REFRESH, timedelta(days=REFRESH_TOKEN_DAYS))


class InvalidToken(Exception):
    pass


def decode_token(token: str, expected_type: str) -> int:
    """id пользователя из токена; InvalidToken — подпись, срок или тип не те."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM],
                             options={"require": ["exp", "sub", "type"]})
    except jwt.PyJWTError as exc:
        raise InvalidToken(str(exc)) from exc
    if payload["type"] != expected_type:
        raise InvalidToken(f"ожидался {expected_type}-токен")
    try:
        return int(payload["sub"])
    except (TypeError, ValueError) as exc:
        raise InvalidToken("некорректный sub") from exc
