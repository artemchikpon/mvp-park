from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.deps import get_db
from api.schemas import PersonOut
from db import stats_service as svc
from db.models import Person

router = APIRouter(prefix="/persons", tags=["persons"])


@router.get("/", response_model=list[PersonOut], summary="Список уникальных лиц")
def list_persons(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return (
        db.query(Person)
        .order_by(Person.first_seen.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.get("/count", summary="Общее количество уникальных лиц в базе")
def count_persons(db: Session = Depends(get_db)):
    return {"count": db.query(Person).count()}


@router.post(
    "/reset",
    summary="Немедленно очистить галерею уникальных лиц (persons)",
    description=(
        "Ручной аналог автоматического ежедневного сброса (12:00 по "
        "таймзоне парка, см. worker._persons_reset_loop). История "
        "пересечений (crossing_events) не удаляется, только теряет "
        "ссылку на удалённый person_id."
    ),
)
def reset_persons(db: Session = Depends(get_db)):
    deleted = svc.reset_persons(db)
    return {"deleted": deleted}
