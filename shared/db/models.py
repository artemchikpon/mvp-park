from sqlalchemy import (
    Column, Integer, String, Date, DateTime, LargeBinary, Boolean, Text,
    UniqueConstraint, ForeignKey, Index, CheckConstraint,
)
from sqlalchemy.orm import relationship
from db.database import Base
from datetime import datetime


class Camera(Base):
    """
    Камеры — URL хранится здесь, не в .env.

    direction — направление, которое ВСЕГДА фиксирует эта камера:
        'in'  — камера стоит на входной группе (Вход)
        'out' — камера стоит на выходной группе (Выход)
    Трекинг траектории/пересечения линии не используется (MVP):
    направление задаётся настройкой камеры один раз при установке.

    gate — название точки/группы Вход+Выход, к которой относится камера,
    например "Центральный" или "Второй (МОРЕ)". Используется для
    группировки и фильтрации статистики по конкретному входу/выходу,
    при этом данные со всех камер агрегируются в общий пул для расчёта
    общей заполненности парка (см. ТЗ, раздел "Синхронизация камер").
    """
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
    """
    Единичное пересечение Вход/Выход — основной "сырой" журнал событий.

    Каждое попадание лица в кадр камеры (после анти-дублирующего cooldown
    на стороне воркера) пишется сюда. Направление и привязка к
    конкретной точке (gate) берутся от камеры на момент события и
    дублируются в строку события, чтобы история не "переписывалась"
    при последующем переназначении камеры.

    На основе этой таблицы считаются все метрики ТЗ:
      - Inflow / Outflow по часам/дням
      - Live Occupancy = sum(in) - sum(out) с начала суток
      - Демография (только по direction='in')
      - Уникальные гости за период (DISTINCT person_id)
    """
    __tablename__ = "crossing_events"

    id = Column(Integer, primary_key=True)
    camera_id = Column(Integer, ForeignKey("cameras.id"), nullable=False)
    direction = Column(String(3), nullable=False)        # 'in' | 'out' (снэпшот камеры)
    gate = Column(String(128), nullable=True)             # снэпшот камеры
    person_id = Column(Integer, ForeignKey("persons.id"), nullable=True)
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


class ParkSettings(Base):
    """
    Единственная строка конфигурации парка (id=1):
    вместимость и порог предупреждения для индикатора Live Occupancy,
    плюс часовой пояс для расчёта границ суток (сброс счётчика в 00:00).
    """
    __tablename__ = "park_settings"

    id = Column(Integer, primary_key=True)
    capacity = Column(Integer, nullable=False, default=5000)
    warning_threshold = Column(Integer, nullable=False, default=4500)
    timezone = Column(String(64), nullable=False, default="UTC")
    updated_at = Column(DateTime, default=datetime.utcnow)
