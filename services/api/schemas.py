from pydantic import BaseModel, field_validator
from typing import Optional, Literal
from datetime import datetime, date


# ── Cameras ──────────────────────────────────────────────────────────────────

Direction = Literal["in", "out"]


class CameraCreate(BaseModel):
    name: str
    url: str
    direction: Direction          # 'in' — камера на Входе, 'out' — камера на Выходе
    gate: Optional[str] = None    # группа/точка, напр. "Центральный", "Второй (МОРЕ)"
    active: bool = True

    @field_validator("url")
    @classmethod
    def url_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("url не может быть пустым")
        return v.strip()


class CameraUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    direction: Optional[Direction] = None
    gate: Optional[str] = None
    active: Optional[bool] = None


class CameraOut(BaseModel):
    id: int
    name: str
    url: str
    direction: Direction
    gate: Optional[str]
    active: bool
    created_at: Optional[datetime]

    model_config = {"from_attributes": True}


# ── Stats / Dashboard ────────────────────────────────────────────────────────

class LiveOccupancyOut(BaseModel):
    occupancy: int
    capacity: int
    warning_threshold: int
    status: Literal["ok", "warning", "full"]
    today_in: int
    today_out: int
    reset_at_utc: str
    gate: Optional[str] = None


class DayTimeseriesOut(BaseModel):
    date: str
    gate: Optional[str] = None
    hourly: list[dict]
    total_in: int
    total_out: int
    peak_in_hour: Optional[int]
    peak_out_hour: Optional[int]
    estimated_session_hours: Optional[int]


class WeekTimeseriesOut(BaseModel):
    week_start: str
    gate: Optional[str] = None
    daily: list[dict]
    busiest_in_day: Optional[str]
    busiest_total_day: Optional[str]


class MonthTimeseriesOut(BaseModel):
    month_start: str
    gate: Optional[str] = None
    daily: list[dict]
    total_unique: int


class DemographicsOut(BaseModel):
    total_in: int
    male: int
    female: int
    male_pct: float
    female_pct: float
    age_groups: dict
    age_groups_pct: dict


class DashboardOut(BaseModel):
    live: LiveOccupancyOut
    day: DayTimeseriesOut
    week: WeekTimeseriesOut
    month: MonthTimeseriesOut
    demographics_today: DemographicsOut


# ── Park settings (вместимость / порог / таймзона) ──────────────────────────

class ParkSettingsOut(BaseModel):
    capacity: int
    warning_threshold: int
    timezone: str
    updated_at: Optional[datetime]

    model_config = {"from_attributes": True}


class ParkSettingsUpdate(BaseModel):
    capacity: Optional[int] = None
    warning_threshold: Optional[int] = None
    timezone: Optional[str] = None

    @field_validator("capacity")
    @classmethod
    def capacity_positive(cls, v):
        if v is not None and v <= 0:
            raise ValueError("capacity должен быть positive")
        return v

    @field_validator("warning_threshold")
    @classmethod
    def threshold_positive(cls, v):
        if v is not None and v <= 0:
            raise ValueError("warning_threshold должен быть positive")
        return v


# ── Persons ───────────────────────────────────────────────────────────────────

class PersonOut(BaseModel):
    id: int
    age: Optional[int]
    gender: Optional[int]          # 1=male, 0=female
    first_seen: Optional[datetime]
    last_seen: Optional[datetime]

    model_config = {"from_attributes": True}
