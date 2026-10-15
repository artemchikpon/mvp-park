from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.deps import get_db
from api.schemas import ParkSettingsOut, ParkSettingsUpdate
from db import stats_service as svc

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/", response_model=ParkSettingsOut,
            summary="Текущие настройки парка (вместимость, порог, таймзона)")
def get_settings(db: Session = Depends(get_db)):
    return svc.get_settings(db)


@router.put("/", response_model=ParkSettingsOut,
            summary="Обновить вместимость / порог предупреждения / таймзону")
def update_settings(body: ParkSettingsUpdate, db: Session = Depends(get_db)):
    try:
        return svc.update_settings(db, **body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
