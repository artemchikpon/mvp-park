"""Первый администратор при пустой таблице users.

Если пользователей ещё нет и заданы ADMIN_USERNAME / ADMIN_PASSWORD,
при старте api создаётся admin с этими данными. Дальше пользователи
управляются через /users, а переменные можно убрать из .env: при
непустой таблице они ни на что не влияют (пароль из .env не перезаписывает
изменённый через API).
"""

import logging

from sqlalchemy.exc import IntegrityError

from api import security
from core.env import env_str
from db.database import SessionLocal
from db.models import User

# Логгер uvicorn уже настроен — строка о создании admin видна в docker logs.
log = logging.getLogger("uvicorn.error")


def ensure_initial_admin() -> None:
    username = env_str("ADMIN_USERNAME", "").strip()
    password = env_str("ADMIN_PASSWORD", "")
    session = SessionLocal()
    try:
        if session.query(User).first() is not None:
            return
        if not username or not password:
            log.warning(
                "Пользователей нет, а ADMIN_USERNAME / ADMIN_PASSWORD не заданы: "
                "вход по JWT невозможен, доступен только X-API-Key"
            )
            return
        if len(password) < 8:
            log.warning("ADMIN_PASSWORD короче 8 символов — первый admin не создан")
            return
        session.add(User(username=username, password_hash=security.hash_password(password), role="admin"))
        try:
            session.commit()
            log.info(f"Создан первый администратор: {username}")
        except IntegrityError:
            # Другая реплика api успела создать его одновременно.
            session.rollback()
    finally:
        session.close()
