from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional

from db.database import SessionLocal
from db.models import Camera
from api.schemas import CameraCreate, CameraUpdate, CameraOut

router = APIRouter(prefix="/cameras", tags=["cameras"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/", response_model=List[CameraOut], summary="Список всех камер")
def list_cameras(
    direction: Optional[str] = None,
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
    cams = db.query(Camera).all()
    gates: dict[str, dict] = {}
    for c in cams:
        key = c.gate or "—"
        g = gates.setdefault(key, {"gate": key, "in_cameras": 0, "out_cameras": 0})
        if c.direction == "in":
            g["in_cameras"] += 1
        else:
            g["out_cameras"] += 1
    return list(gates.values())


@router.get("/{camera_id}", response_model=CameraOut, summary="Камера по ID")
def get_camera(camera_id: int, db: Session = Depends(get_db)):
    cam = db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(status_code=404, detail="Камера не найдена")
    return cam


@router.post("/", response_model=CameraOut, status_code=201, summary="Добавить камеру")
def create_camera(body: CameraCreate, db: Session = Depends(get_db)):
    cam = Camera(**body.model_dump())
    db.add(cam)
    db.commit()
    db.refresh(cam)
    return cam


@router.patch("/{camera_id}", response_model=CameraOut, summary="Обновить камеру")
def update_camera(camera_id: int, body: CameraUpdate, db: Session = Depends(get_db)):
    cam = db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(status_code=404, detail="Камера не найдена")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(cam, field, value)
    db.commit()
    db.refresh(cam)
    return cam


@router.delete("/{camera_id}", status_code=204, summary="Удалить камеру")
def delete_camera(camera_id: int, db: Session = Depends(get_db)):
    cam = db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(status_code=404, detail="Камера не найдена")
    db.delete(cam)
    db.commit()


@router.post("/{camera_id}/toggle", response_model=CameraOut, summary="Включить / выключить камеру")
def toggle_camera(camera_id: int, db: Session = Depends(get_db)):
    cam = db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(status_code=404, detail="Камера не найдена")
    cam.active = not cam.active
    db.commit()
    db.refresh(cam)
    return cam
