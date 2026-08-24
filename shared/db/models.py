from sqlalchemy import (
    Column, Integer, String, Date, DateTime, LargeBinary, Boolean, Text,
    ForeignKey, Index, CheckConstraint,
)
from sqlalchemy.orm import relationship
from db.database import Base
from datetime import datetime


class Camera(Base):

    __tablename__ = "cameras"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)          # "Вход северный"
    url = Column(Text, nullable=False)                  # rtsp://...
    direction = Column(String(3), nullable=False)        # 'in' | 'out'
    gate = Column(String(128), nullable=True)            # "Центральный", "Второй (МОРЕ)" ...
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("direction in ('in','out')", name="ck_camera_direction"),
    )


class Person(Base):
    """Уникальное лицо — сохраняется один раз при первом появлении"""
    __tablename__ = "persons"

    id = Column(Integer, primary_key=True)
    embedding = Column(LargeBinary, nullable=False)     # усреднённый вектор (bytes)
    age = Column(Integer)
    gender = Column(Integer)                            # 1=male, 0=female
    first_seen = Column(DateTime, default=datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.utcnow)


class CrossingEvent(Base):

    __tablename__ = "crossing_events"

    id = Column(Integer, primary_key=True)
    camera_id = Column(Integer, ForeignKey("cameras.id"), nullable=False)
    direction = Column(String(3), nullable=False)        # 'in' | 'out' (снэпшот камеры)
    gate = Column(String(128), nullable=True)             # снэпшот камеры
    person_id = Column(Integer, ForeignKey("persons.id", ondelete="SET NULL"), nullable=True)
    age = Column(Integer, nullable=True)                  # возраст в момент пересечения
    gender = Column(Integer, nullable=True)               # пол в момент пересечения
    ts = Column(DateTime, default=datetime.utcnow, nullable=False)

    camera = relationship("Camera")
    person = relationship("Person")

    __table_args__ = (
        CheckConstraint("direction in ('in','out')", name="ck_event_direction"),
        Index("ix_crossing_events_ts", "ts"),
        Index("ix_crossing_events_camera_ts", "camera_id", "ts"),
        Index("ix_crossing_events_direction_ts", "direction", "ts"),
    )


class Employee(Base):

    __tablename__ = "employees"

    id = Column(Integer, primary_key=True)
    external_id = Column(Integer, nullable=True, unique=True)   # id в HR
    first_name = Column(String(128), nullable=False)
    file_id = Column(Integer, nullable=True)
    embedding = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class ParkSettings(Base):

    __tablename__ = "park_settings"

    id = Column(Integer, primary_key=True)
    capacity = Column(Integer, nullable=False, default=5000)
    warning_threshold = Column(Integer, nullable=False, default=4500)
    timezone = Column(String(64), nullable=False, default="UTC")
    updated_at = Column(DateTime, default=datetime.utcnow)
