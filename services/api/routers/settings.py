from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from db.database import SessionLocal
from db import stats_service as svc
from api.schemas import ParkSettingsOut, ParkSettingsUpdate

router = APIRouter(prefix="/settings", tags=["settings"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/", response_model=ParkSettingsOut,
            summary="Текущие настройки парка (вместимость, порог, таймзона)")
def get_settings(db: Session = Depends(get_db)):
    return svc.get_settings(db)


@router.put("/", response_model=ParkSettingsOut,
            summary="Обновить вместимость / порог предупреждения / таймзону")
def update_settings(body: ParkSettingsUpdate, db: Session = Depends(get_db)):
    return svc.update_settings(db, **body.model_dump(exclude_none=True))
