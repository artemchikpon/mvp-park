from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import Optional
from datetime import date

from db.database import SessionLocal
from db import stats_service as svc
from api.schemas import (
    LiveOccupancyOut, DayTimeseriesOut, WeekTimeseriesOut,
    MonthTimeseriesOut, DemographicsOut, DashboardOut,
)

router = APIRouter(prefix="/stats", tags=["stats"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/live", response_model=LiveOccupancyOut,
            summary="Текущая заполненность парка (Live Occupancy)")
def live(
    gate: Optional[str] = Query(None, description="Фильтр по точке (gate), напр. 'Центральный'"),
    db: Session = Depends(get_db),
):
    return svc.get_live_occupancy(db, gate=gate)


@router.get("/day", response_model=DayTimeseriesOut,
            summary="График по часам за день + пики входа/выхода")
def day(
    day: Optional[date] = Query(None, description="Дата (YYYY-MM-DD), по умолчанию сегодня"),
    gate: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    return svc.get_day_timeseries(db, day or svc.current_park_date(db), gate=gate)


@router.get("/week", response_model=WeekTimeseriesOut,
            summary="График по дням за неделю, содержащую указанную дату")
def week(
    day: Optional[date] = Query(None, description="Любая дата внутри нужной недели"),
    gate: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    return svc.get_week_timeseries(db, day or svc.current_park_date(db), gate=gate)


@router.get("/month", response_model=MonthTimeseriesOut,
            summary="Накопленный итог уникальных гостей за месяц")
def month(
    day: Optional[date] = Query(None, description="Любая дата внутри нужного месяца"),
    gate: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    return svc.get_month_timeseries(db, day or svc.current_park_date(db), gate=gate)


@router.get("/demographics", response_model=DemographicsOut,
            summary="Пол и возрастные группы (только по направлению Вход)")
def demographics(
    period: str = Query("day", pattern="^(day|week|month)$"),
    day: Optional[date] = Query(None),
    gate: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    start_utc, end_utc = svc.period_bounds(db, period, day or svc.current_park_date(db))
    return svc.get_demographics(db, start_utc, end_utc, gate=gate)


@router.get("/dashboard", response_model=DashboardOut,
            summary="Все данные для One-Page Dashboard за один вызов")
def dashboard(
    day: Optional[date] = Query(None),
    gate: Optional[str] = Query(None, description="Фильтр по конкретной точке Вход/Выход"),
    db: Session = Depends(get_db),
):
    return svc.get_dashboard(db, day or svc.current_park_date(db), gate=gate)
