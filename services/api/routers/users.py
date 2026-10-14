"""Пользователи администрации (только для admin)."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api import security
from api.auth import Principal, require_admin
from api.deps import get_db
from api.schemas import UserCreate, UserOut, UserUpdate
from db.models import User

router = APIRouter(prefix="/users", tags=["users"])


def _get_or_404(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return user


def _forbid_self_lockout(principal: Principal, user: User, body: UserUpdate | None = None) -> None:
    """Admin не может удалить, отключить или понизить сам себя — иначе легко
    остаться без единого администратора."""
    if principal.user_id != user.id:
        return
    if body is None or body.is_active is False or (body.role is not None and body.role != "admin"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Нельзя удалить, отключить или понизить свою учётную запись",
        )


@router.get("/", response_model=list[UserOut], summary="Список пользователей")
def list_users(db: Session = Depends(get_db), _: Principal = Depends(require_admin)):
    return db.query(User).order_by(User.id).all()


@router.post("/", response_model=UserOut, status_code=201, summary="Создать пользователя")
def create_user(body: UserCreate, db: Session = Depends(get_db), _: Principal = Depends(require_admin)):
    user = User(username=body.username, password_hash=security.hash_password(body.password), role=body.role)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Пользователь с таким логином уже есть")
    db.refresh(user)
    return user


@router.patch("/{user_id}", response_model=UserOut, summary="Сменить пароль, роль или включить/отключить")
def update_user(
    user_id: int,
    body: UserUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
):
    user = _get_or_404(db, user_id)
    _forbid_self_lockout(principal, user, body)
    if body.password is not None:
        user.password_hash = security.hash_password(body.password)
    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    db.commit()
    db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=204, summary="Удалить пользователя")
def delete_user(user_id: int, db: Session = Depends(get_db), principal: Principal = Depends(require_admin)):
    user = _get_or_404(db, user_id)
    _forbid_self_lockout(principal, user)
    db.delete(user)
    db.commit()
