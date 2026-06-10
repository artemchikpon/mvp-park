from sqlalchemy import Column, Integer, Date, DateTime
from db.database import Base
from datetime import datetime


class DailyStats(Base):
    __tablename__ = "daily_stats"

    id = Column(Integer, primary_key=True)

    date = Column(Date, unique=True)

    total = Column(Integer, default=0)
    new_faces = Column(Integer, default=0)
    known_faces = Column(Integer, default=0)

    male = Column(Integer, default=0)
    female = Column(Integer, default=0)

    kids = Column(Integer, default=0)
    teens = Column(Integer, default=0)
    adults = Column(Integer, default=0)
    middle = Column(Integer, default=0)
    elderly = Column(Integer, default=0)

    updated_at = Column(DateTime)