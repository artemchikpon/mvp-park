"""
Бизнес-логика статистики посещаемости (раздел ТЗ "Аналитический модуль MVP").

Источник истины — таблица crossing_events: одна строка = одно пересечение
линии Вход/Выход одним человеком на одной камере. Направление берётся из
настройки камеры (без трекинга траектории — см. обсуждение MVP).

Все агрегаты (Live Occupancy, Inflow/Outflow, демография, пики, уникальные
гости за месяц) считаются "по требованию" прямо из этой таблицы — для
объёма MVP это быстрее и надёжнее, чем поддерживать отдельные
предрасчитанные таблицы, которые могут разойтись с сырыми данными.
"""

from collections import defaultdict, Counter
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from db.database import SessionLocal
from db.models import CrossingEvent, ParkSettings, Person


# ── Возрастные группы (строго по ТЗ, раздел 2, Блок А) ──────────────────────
#   Дети:     0–12
#   Подростки: 13–18
#   Взрослые:  19–60
#   Пожилые:   60+

def group_age(age):
    if age is None or age < 0:
        return None
    if age <= 12:
        return "kids"
    if age <= 18:
        return "teens"
    if age <= 60:
        return "adults"
    return "elderly"


AGE_GROUP_KEYS = ("kids", "teens", "adults", "elderly")


# ── Настройки парка ───────────────────────────────────────────────────────

def get_settings(db: Session) -> ParkSettings:
    s = db.get(ParkSettings, 1)
    if s:
        return s

    s = ParkSettings(id=1, capacity=5000, warning_threshold=4500, timezone="UTC")
    db.add(s)
    try:
        db.commit()
        db.refresh(s)
        return s
    except IntegrityError:
        # Другая реплика успела создать singleton row одновременно.
        db.rollback()
        existing = db.get(ParkSettings, 1)
        if existing is None:
            raise
        return existing


def update_settings(db: Session, **fields) -> ParkSettings:
    s = get_settings(db)
    new_capacity = fields.get("capacity") if fields.get("capacity") is not None else s.capacity
    new_threshold = fields.get("warning_threshold") if fields.get("warning_threshold") is not None else s.warning_threshold
    new_timezone = fields.get("timezone") if fields.get("timezone") is not None else s.timezone

    if new_capacity <= 0:
        raise ValueError("capacity должен быть положительным")
    if new_threshold <= 0:
        raise ValueError("warning_threshold должен быть положительным")
    if new_threshold > new_capacity:
        raise ValueError("warning_threshold не может быть больше capacity")
    try:
        ZoneInfo(new_timezone)
    except Exception as exc:
        raise ValueError(f"Невалидная timezone: {new_timezone}") from exc

    for k, v in fields.items():
        if v is not None:
            setattr(s, k, v)
    s.updated_at = datetime.utcnow()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise
    db.refresh(s)
    return s


def current_park_date(db: Session) -> date:
    tz = _tz(db)
    return datetime.now(tz).date()


def _tz(db: Session) -> ZoneInfo:
    return ZoneInfo(get_settings(db).timezone)


def local_midnight_to_utc(local_day: date, tz: ZoneInfo) -> datetime:
    """Начало суток (00:00) в таймзоне парка, переведённое в naive UTC
    (так как ts в БД хранится как naive UTC datetime.utcnow())."""
    local_dt = datetime.combine(local_day, time.min, tzinfo=tz)
    return local_dt.astimezone(timezone.utc).replace(tzinfo=None)


def _to_local(ts: datetime, tz: ZoneInfo) -> datetime:
    return ts.replace(tzinfo=timezone.utc).astimezone(tz)


# ── Запись события (вызывается воркером на каждое распознанное пересечение) ─

def record_crossing(db: Session, camera, person_id, age, gender, ts=None) -> CrossingEvent:
    event = CrossingEvent(
        camera_id=camera.id,
        direction=camera.direction,
        gate=camera.gate,
        person_id=person_id,
        age=age if (age is not None and age >= 0) else None,
        gender=gender if gender in (0, 1) else None,
        ts=ts or datetime.utcnow(),
    )
    db.add(event)
    db.commit()
    return event


def log_crossing(camera, person_id, age, gender, ts=None) -> bool:
    """Записывает событие и возвращает True только после успешного COMMIT."""
    session = SessionLocal()
    try:
        record_crossing(session, camera, person_id, age, gender, ts)
        return True
    except Exception as e:
        session.rollback()
        print("DB ERROR (log_crossing):", e)
        return False
    finally:
        session.close()


# ── Ежедневный сброс галереи лиц (Person) ────────────────────────────────────
#
# persons — это не история посещений, а "рабочая" галерея векторов для
# распознавания "тот же human или новый" (см. FaceStoragePG.identify).
# По требованию продукта эта галерея должна каждый день начинаться с нуля
# (в 12:00 по таймзоне парка, см. worker._persons_reset_loop), т.е. один и
# тот же посетитель, пришедший сегодня и завтра, будет распознан как два
# разных Person.
#
# crossing_events (сама история пересечений — сколько, когда, откуда) не
# трогаем: только отвязываем от неё удаляемые person_id (age/gender на
# момент пересечения уже сохранены в самой строке события и не теряются).

def reset_persons(db: Session) -> int:
    """Полностью очищает таблицу persons (embedding-векторы уникальных лиц).

    Возвращает количество удалённых записей. Историю crossing_events не
    удаляет — только обнуляет ссылку person_id у уже прошедших событий,
    чтобы не нарушить внешний ключ.
    """
    db.query(CrossingEvent).filter(CrossingEvent.person_id.isnot(None)).update(
        {CrossingEvent.person_id: None}, synchronize_session=False
    )
    deleted = db.query(Person).delete(synchronize_session=False)
    db.commit()
    return deleted


# ── Live Occupancy (раздел ТЗ "Блок Б") ──────────────────────────────────────

def get_live_occupancy(db: Session, gate: str | None = None) -> dict:
    settings = get_settings(db)
    tz = ZoneInfo(settings.timezone)
    today_local = datetime.now(tz).date()
    start_utc = local_midnight_to_utc(today_local, tz)
    now_utc = datetime.utcnow()

    base = db.query(CrossingEvent).filter(
        CrossingEvent.ts >= start_utc,
        CrossingEvent.ts <= now_utc,
    )
    if gate:
        base = base.filter(CrossingEvent.gate == gate)

    total_in = base.filter(CrossingEvent.direction == "in").count()
    total_out = base.filter(CrossingEvent.direction == "out").count()

    # Защита от рассинхрона камер (например, если одна из камер временно
    # не работала) — занятость не может быть отрицательной.
    occupancy = max(total_in - total_out, 0)

    if occupancy >= settings.capacity:
        status = "full"
    elif occupancy >= settings.warning_threshold:
        status = "warning"
    else:
        status = "ok"

    return {
        "occupancy": occupancy,
        "capacity": settings.capacity,
        "warning_threshold": settings.warning_threshold,
        "status": status,          # ok | warning | full — для цвета виджета
        "today_in": total_in,
        "today_out": total_out,
        "reset_at_utc": start_utc.isoformat() + "Z",
        "gate": gate,
    }


# ── День: график по часам + пики (раздел ТЗ "3. Структура Дашборда") ───────

def get_day_timeseries(db: Session, day: date, gate: str | None = None) -> dict:
    tz = _tz(db)
    start_utc = local_midnight_to_utc(day, tz)
    end_utc = local_midnight_to_utc(day + timedelta(days=1), tz)

    q = db.query(CrossingEvent.direction, CrossingEvent.ts).filter(
        CrossingEvent.ts >= start_utc, CrossingEvent.ts < end_utc
    )
    if gate:
        q = q.filter(CrossingEvent.gate == gate)

    hourly_in = [0] * 24
    hourly_out = [0] * 24
    for direction, ts in q.all():
        h = _to_local(ts, tz).hour
        if direction == "in":
            hourly_in[h] += 1
        else:
            hourly_out[h] += 1

    peak_in_hour = max(range(24), key=lambda h: hourly_in[h]) if any(hourly_in) else None
    peak_out_hour = max(range(24), key=lambda h: hourly_out[h]) if any(hourly_out) else None

    # "Длина сессии (оценка)" — разница между пиком входа и пиком выхода.
    # Если выход "раньше" входа в течение суток — оценка не имеет смысла.
    session_hours = None
    if peak_in_hour is not None and peak_out_hour is not None and peak_out_hour >= peak_in_hour:
        session_hours = peak_out_hour - peak_in_hour

    return {
        "date": day.isoformat(),
        "gate": gate,
        "hourly": [
            {"hour": h, "in": hourly_in[h], "out": hourly_out[h]}
            for h in range(24)
        ],
        "total_in": sum(hourly_in),
        "total_out": sum(hourly_out),
        "peak_in_hour": peak_in_hour,
        "peak_out_hour": peak_out_hour,
        "estimated_session_hours": session_hours,
    }


# ── Неделя: график по дням + "входной день" (раздел ТЗ "3. ... Неделя") ────

def get_week_timeseries(db: Session, any_day: date, gate: str | None = None) -> dict:
    tz = _tz(db)
    week_start = any_day - timedelta(days=any_day.weekday())  # понедельник
    start_utc = local_midnight_to_utc(week_start, tz)
    end_utc = local_midnight_to_utc(week_start + timedelta(days=7), tz)

    q = db.query(CrossingEvent.direction, CrossingEvent.ts).filter(
        CrossingEvent.ts >= start_utc, CrossingEvent.ts < end_utc
    )
    if gate:
        q = q.filter(CrossingEvent.gate == gate)

    daily_in = defaultdict(int)
    daily_out = defaultdict(int)
    for direction, ts in q.all():
        d = _to_local(ts, tz).date()
        if direction == "in":
            daily_in[d] += 1
        else:
            daily_out[d] += 1

    daily = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        daily.append({
            "date": d.isoformat(),
            "weekday": d.isoweekday(),  # 1=Пн .. 7=Вс
            "in": daily_in.get(d, 0),
            "out": daily_out.get(d, 0),
        })

    busiest_in = max(daily, key=lambda r: r["in"]) if any(r["in"] for r in daily) else None
    busiest_total = max(daily, key=lambda r: r["in"] + r["out"]) if any(r["in"] + r["out"] for r in daily) else None

    return {
        "week_start": week_start.isoformat(),
        "gate": gate,
        "daily": daily,
        "busiest_in_day": busiest_in["date"] if busiest_in else None,
        "busiest_total_day": busiest_total["date"] if busiest_total else None,
    }


# ── Месяц: накопленный итог уникальных гостей (раздел ТЗ "3. ... Месяц") ───

def get_month_timeseries(db: Session, any_day: date, gate: str | None = None) -> dict:
    tz = _tz(db)
    month_start = any_day.replace(day=1)
    if month_start.month == 12:
        next_month_start = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month_start = month_start.replace(month=month_start.month + 1)

    start_utc = local_midnight_to_utc(month_start, tz)
    end_utc = local_midnight_to_utc(next_month_start, tz)

    q = db.query(CrossingEvent.person_id, CrossingEvent.ts).filter(
        CrossingEvent.direction == "in",
        CrossingEvent.person_id.isnot(None),
        CrossingEvent.ts >= start_utc,
        CrossingEvent.ts < end_utc,
    )
    if gate:
        q = q.filter(CrossingEvent.gate == gate)

    # Для каждого уникального гостя — дата его ПЕРВОГО визита в этом месяце.
    first_seen_day: dict[int, date] = {}
    for person_id, ts in q.all():
        d = _to_local(ts, tz).date()
        if person_id not in first_seen_day or d < first_seen_day[person_id]:
            first_seen_day[person_id] = d

    new_unique_by_day = Counter(first_seen_day.values())

    # Текущий месяц показываем только до сегодняшнего дня; исторический
    # месяц всегда полный, независимо от того, какую дату внутри месяца
    # передал клиент.
    current_local_date = datetime.now(tz).date()
    if month_start.year == current_local_date.year and month_start.month == current_local_date.month:
        last_day = current_local_date
    else:
        last_day = next_month_start - timedelta(days=1)
    daily = []
    cumulative = 0
    d = month_start
    while d <= last_day:
        new_today = new_unique_by_day.get(d, 0)
        cumulative += new_today
        daily.append({
            "date": d.isoformat(),
            "new_unique": new_today,
            "cumulative_unique": cumulative,
        })
        d += timedelta(days=1)

    return {
        "month_start": month_start.isoformat(),
        "gate": gate,
        "daily": daily,
        "total_unique": cumulative,
    }


# ── Демография — только по направлению IN (раздел ТЗ "Блок А") ────────────

def get_demographics(db: Session, start_utc: datetime, end_utc: datetime, gate: str | None = None) -> dict:
    q = db.query(CrossingEvent.age, CrossingEvent.gender).filter(
        CrossingEvent.direction == "in",
        CrossingEvent.ts >= start_utc,
        CrossingEvent.ts < end_utc,
    )
    if gate:
        q = q.filter(CrossingEvent.gate == gate)

    rows = q.all()
    total = len(rows)
    male = sum(1 for _, gender in rows if gender == 1)
    female = sum(1 for _, gender in rows if gender == 0)

    groups = {k: 0 for k in AGE_GROUP_KEYS}
    for age, _ in rows:
        g = group_age(age)
        if g:
            groups[g] += 1

    def pct(x):
        return round(x / total * 100, 1) if total else 0.0

    return {
        "total_in": total,
        "male": male,
        "female": female,
        "male_pct": pct(male),
        "female_pct": pct(female),
        "age_groups": groups,
        "age_groups_pct": {k: pct(v) for k, v in groups.items()},
    }


def period_bounds(db: Session, period: str, any_day: date) -> tuple[datetime, datetime]:
    """Границы периода (в UTC) для демографии: day | week | month."""
    tz = _tz(db)
    if period == "week":
        start_local = any_day - timedelta(days=any_day.weekday())
        end_local = start_local + timedelta(days=7)
    elif period == "month":
        start_local = any_day.replace(day=1)
        if start_local.month == 12:
            end_local = start_local.replace(year=start_local.year + 1, month=1)
        else:
            end_local = start_local.replace(month=start_local.month + 1)
    else:  # day
        start_local = any_day
        end_local = any_day + timedelta(days=1)
    return local_midnight_to_utc(start_local, tz), local_midnight_to_utc(end_local, tz)


# ── Сводный дашборд — один вызов под "One-Page Dashboard" из ТЗ ────────────

def get_dashboard(db: Session, today: date, gate: str | None = None) -> dict:
    start_utc, end_utc = period_bounds(db, "day", today)
    return {
        "live": get_live_occupancy(db, gate=gate),
        "day": get_day_timeseries(db, today, gate=gate),
        "week": get_week_timeseries(db, today, gate=gate),
        "month": get_month_timeseries(db, today, gate=gate),
        "demographics_today": get_demographics(db, start_utc, end_utc, gate=gate),
    }
