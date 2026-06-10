from db.database import SessionLocal
from db.models import DailyStats
from datetime import datetime


def group_age(age):
    if age < 18:
        return "kids"
    if age < 25:
        return "teens"
    if age < 45:
        return "adults"
    if age < 65:
        return "middle"
    return "elderly"


def update_stats(age, gender, is_new):

    session = SessionLocal()

    try:
        today = datetime.utcnow().date()

        stat = session.query(DailyStats).filter(
            DailyStats.date == today
        ).first()

        if not stat:
            stat = DailyStats(date=today)
            session.add(stat)
            session.commit()
            session.refresh(stat)

        stat.total += 1

        if is_new:
            stat.new_faces += 1
        else:
            stat.known_faces += 1

        if gender == 1:
            stat.male += 1
        else:
            stat.female += 1

        group = group_age(age)

        setattr(stat, group, getattr(stat, group) + 1)

        stat.updated_at = datetime.utcnow()

        session.commit()

    except Exception as e:
        session.rollback()
        print("DB ERROR:", e)

    finally:
        session.close()