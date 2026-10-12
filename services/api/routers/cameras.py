from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.deps import get_db
from api.schemas import CameraCreate, CameraOut, CameraUpdate
from db.models import Camera

router = APIRouter(prefix="/cameras", tags=["cameras"])

NO_GATE = "—"


def _get_or_404(db: Session, camera_id: int) -> Camera:
    cam = db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(status_code=404, detail="Камера не найдена")
    return cam


def _save(db: Session, cam: Camera) -> Camera:
    db.commit()
    db.refresh(cam)
    return cam


@router.get("/", response_model=list[CameraOut], summary="Список всех камер")
def list_cameras(
    direction: Optional[Literal["in", "out"]] = None,
    gate: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(Camera).order_by(Camera.id)
    if direction:
        q = q.filter(Camera.direction == direction)
    if gate:
        q = q.filter(Camera.gate == gate)
    return q.all()


@router.get("/gates", summary="Список точек (gate) с количеством камер Вход/Выход")
def list_gates(db: Session = Depends(get_db)):
    """Для фильтра на дашборде: какие 'точки' (Центральный, Второй (МОРЕ)...)
    есть в системе и сколько у каждой камер на вход/выход."""
    gates: dict[str, dict] = {}
    for c in db.query(Camera).all():
        key = c.gate or NO_GATE
        g = gates.setdefault(key, {"gate": key, "in_cameras": 0, "out_cameras": 0})
        g["in_cameras" if c.direction == "in" else "out_cameras"] += 1
    return list(gates.values())


@router.get("/{camera_id}", response_model=CameraOut, summary="Камера по ID")
def get_camera(camera_id: int, db: Session = Depends(get_db)):
    return _get_or_404(db, camera_id)


@router.post("/", response_model=CameraOut, status_code=201, summary="Добавить камеру")
def create_camera(body: CameraCreate, db: Session = Depends(get_db)):
    cam = Camera(**body.model_dump())
    db.add(cam)
    return _save(db, cam)


@router.patch("/{camera_id}", response_model=CameraOut, summary="Обновить камеру")
def update_camera(camera_id: int, body: CameraUpdate, db: Session = Depends(get_db)):
    cam = _get_or_404(db, camera_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(cam, field, value)
    return _save(db, cam)


@router.delete("/{camera_id}", status_code=204, summary="Удалить камеру")
def delete_camera(camera_id: int, db: Session = Depends(get_db)):
    cam = _get_or_404(db, camera_id)
    db.delete(cam)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Нельзя удалить камеру: по ней уже есть события. Сначала деактивируйте камеру.",
        )


@router.post("/{camera_id}/toggle", response_model=CameraOut, summary="Включить / выключить камеру")
def toggle_camera(camera_id: int, db: Session = Depends(get_db)):
    cam = _get_or_404(db, camera_id)
    cam.active = not cam.active
    return _save(db, cam)
