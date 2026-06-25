import logging
import os
import numpy as np
import httpx
import cv2

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime

from db.database import SessionLocal
from db.models import Employee

log = logging.getLogger("employees_router")

router = APIRouter(prefix="/employees", tags=["employees"])

HR_BASE = os.environ.get("HR_API_BASE", "https://192.168.0.146:4050")
SSL_VERIFY = os.environ.get("HR_SSL_VERIFY", "false").lower() != "false"
SIMILARITY = 0.65
EMB_DTYPE = np.float32



def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()



class EmployeeOut(BaseModel):
    id: int
    external_id: Optional[int]
    first_name: str
    file_id: Optional[int]
    created_at: Optional[datetime]

    model_config = {"from_attributes": True}


class AddEmployeeRequest(BaseModel):
    first_name: str
    file_id: int
    external_id: Optional[int] = None


class SyncResult(BaseModel):
    added: int
    updated: int
    skipped: int
    errors: int



def _emb_to_bytes(vec: np.ndarray) -> bytes:
    return vec.astype(EMB_DTYPE).tobytes()


def _fetch_frame_from_file(file_id: int) -> np.ndarray:
    """Скачивает фото по file_id из HR API и возвращает BGR frame."""
    url = f"{HR_BASE}/api/v1/files/{file_id}/view"
    try:
        resp = httpx.get(url, verify=SSL_VERIFY, timeout=15)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502,
            detail=f"HR API вернул ошибку для file_id={file_id}: {e.response.status_code}"
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Не удалось скачать фото: {e}")

    img_array = np.frombuffer(resp.content, dtype=np.uint8)
    frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(
            status_code=422,
            detail=f"Не удалось декодировать изображение file_id={file_id}"
        )
    return frame


def _extract_embedding(frame: np.ndarray) -> np.ndarray:
    try:
        from face_engine import extract_faces
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="face_engine недоступен в этом сервисе. "
                   "Используйте POST /employees/sync через worker."
        )

    faces = extract_faces(frame)
    if not faces:
        raise HTTPException(
            status_code=422,
            detail="Лицо не найдено на фотографии. Убедитесь что фото чёткое и лицо хорошо видно."
        )

    face = faces[0]
    if face.embedding is None:
        raise HTTPException(status_code=422, detail="Не удалось извлечь embedding лица")

    return face.embedding


# ── Роуты ─────────────────────────────────────────────────────────────────────

@router.get("/", response_model=list[EmployeeOut], summary="Список сотрудников")
def list_employees(db: Session = Depends(get_db)):
    """Возвращает всех сотрудников в БД (без векторов)."""
    return db.query(Employee).order_by(Employee.created_at.desc()).all()


@router.post("/sync", response_model=SyncResult, summary="Синхронизировать всех сотрудников из HR API")
def sync_employees_route():
    """
    Запускает полную синхронизацию: загружает список сотрудников из HR API,
    скачивает фото каждого и сохраняет/обновляет векторы в БД.

    Сотрудники с file=0 (нет фото) пропускаются.
    """
    # Импортируем здесь чтобы не тянуть face_engine при импорте модуля
    try:
        from employee_sync import sync_employees
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="employee_sync недоступен. Этот эндпоинт работает только в worker-сервисе."
        )

    result = sync_employees()
    return result


@router.post("/add", response_model=EmployeeOut, summary="Добавить сотрудника вручную")
def add_employee(body: AddEmployeeRequest, db: Session = Depends(get_db)):
    """
    Добавляет одного сотрудника по firstname и file_id.

    Скачивает фото, извлекает вектор лица и сохраняет в БД.
    Если сотрудник с таким external_id уже есть — обновляет его вектор.
    """
    # Проверяем нет ли уже такого external_id
    if body.external_id is not None:
        existing = db.query(Employee).filter(
            Employee.external_id == body.external_id
        ).first()
        if existing:
            # Обновляем
            frame = _fetch_frame_from_file(body.file_id)
            emb = _extract_embedding(frame)
            existing.embedding = _emb_to_bytes(emb)
            existing.first_name = body.first_name
            existing.file_id = body.file_id
            db.commit()
            db.refresh(existing)
            return existing

    # Новый сотрудник
    frame = _fetch_frame_from_file(body.file_id)
    emb = _extract_embedding(frame)

    emp = Employee(
        external_id=body.external_id,
        first_name=body.first_name,
        file_id=body.file_id,
        embedding=_emb_to_bytes(emb),
    )
    db.add(emp)
    db.commit()
    db.refresh(emp)
    return emp


@router.delete(
    "/{employee_id}",
    summary="Удалить сотрудника и его векторы из БД",
)
def delete_employee(employee_id: int, db: Session = Depends(get_db)):
    """
    Удаляет сотрудника по его id в нашей БД (не external_id из HR).
    После удаления этот человек снова будет считаться как посетитель.
    """
    emp = db.query(Employee).filter(Employee.id == employee_id).first()
    if not emp:
        raise HTTPException(
            status_code=404,
            detail=f"Сотрудник id={employee_id} не найден"
        )

    name = emp.first_name
    db.delete(emp)
    db.commit()
    return {"ok": True, "deleted_id": employee_id, "first_name": name}
