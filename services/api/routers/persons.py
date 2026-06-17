from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import List, Optional

from db.database import SessionLocal
from db.models import Person
from api.schemas import PersonOut

router = APIRouter(prefix="/persons", tags=["persons"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/", response_model=List[PersonOut], summary="Список уникальных лиц")
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
