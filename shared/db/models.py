from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer,
    LargeBinary, String, Text,
)
from sqlalchemy.orm import relationship

from core.timeutil import utcnow
from db.database import Base

_DIRECTION_CHECK = "direction in ('in','out')"


class Camera(Base):
    __tablename__ = "cameras"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)          # "Вход северный"
    url = Column(Text, nullable=False)                  # rtsp://...
    direction = Column(String(3), nullable=False)       # 'in' | 'out'
    gate = Column(String(128), nullable=True)           # "Центральный", "Второй (МОРЕ)" ...
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint(_DIRECTION_CHECK, name="ck_camera_direction"),
    )


class Person(Base):
    """Уникальное лицо в рабочей галерее распознавания (очищается раз в сутки)."""
    __tablename__ = "persons"

    id = Column(Integer, primary_key=True)
    embedding = Column(LargeBinary, nullable=False)     # float32-вектор (bytes)
    age = Column(Integer)
    gender = Column(Integer)                            # 1=male, 0=female
    first_seen = Column(DateTime, default=utcnow)
    last_seen = Column(DateTime, default=utcnow)


class CrossingEvent(Base):
    """Одно пересечение линии Вход/Выход одним человеком на одной камере."""
    __tablename__ = "crossing_events"

    id = Column(Integer, primary_key=True)
    camera_id = Column(Integer, ForeignKey("cameras.id"), nullable=False)
    direction = Column(String(3), nullable=False)       # снэпшот камеры
    gate = Column(String(128), nullable=True)           # снэпшот камеры
    person_id = Column(Integer, ForeignKey("persons.id", ondelete="SET NULL"), nullable=True)
    age = Column(Integer, nullable=True)                # возраст в момент пересечения
    gender = Column(Integer, nullable=True)             # пол в момент пересечения
    ts = Column(DateTime, default=utcnow, nullable=False)

    camera = relationship("Camera")
    person = relationship("Person")

    __table_args__ = (
        CheckConstraint(_DIRECTION_CHECK, name="ck_event_direction"),
        Index("ix_crossing_events_ts", "ts"),
        Index("ix_crossing_events_camera_ts", "camera_id", "ts"),
        Index("ix_crossing_events_direction_ts", "direction", "ts"),
    )


class Employee(Base):
    """Сотрудник из HR-системы: его лицо не учитывается в статистике."""
    __tablename__ = "employees"

    id = Column(Integer, primary_key=True)
    external_id = Column(Integer, nullable=True, unique=True)   # id в HR
    first_name = Column(String(128), nullable=False)
    file_id = Column(Integer, nullable=True)
    embedding = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime, default=utcnow)


class ParkSettings(Base):
    """Singleton-строка (id=1) с настройками парка."""
    __tablename__ = "park_settings"

    id = Column(Integer, primary_key=True)
    capacity = Column(Integer, nullable=False, default=5000)
    warning_threshold = Column(Integer, nullable=False, default=4500)
    timezone = Column(String(64), nullable=False, default="UTC")
    updated_at = Column(DateTime, default=utcnow)
