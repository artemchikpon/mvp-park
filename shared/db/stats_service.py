"""
Бизнес-логика статистики посещаемости (раздел ТЗ "Аналитический модуль MVP").

Источник истины — таблица crossing_events: одна строка = одно пересечение
линии Вход/Выход одним человеком на одной камере. Направление берётся из
настройки камеры (без трекинга траектории — см. обсуждение MVP).

Все агрегаты (Live Occupancy, Inflow/Outflow, демография, пики, уникальные
гости за месяц) считаются "по требованию" прямо из этой таблицы — для
объёма MVP это быстрее и надёжнее, чем поддерживать отдельные
предрасчитанные таблицы, которые могут разойтись с сырыми данными.

Время в БД — naive UTC; границы суток/недель/месяцев считаются в таймзоне
парка (park_settings.timezone) и переводятся в UTC перед запросом.
"""

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query, Session

from core.timeutil import DEFAULT_PARK_TIMEZONE, utcnow
from db.database import SessionLocal
from db.models import CrossingEvent, ParkSettings, Person

SETTINGS_ID = 1
DEFAULT_SETTINGS = {"capacity": 5000, "warning_threshold": 4500, "timezone": DEFAULT_PARK_TIMEZONE}


# ── Возрастные группы (строго по ТЗ, раздел 2, Блок А) ──────────────────────
#   Дети:      0–12
#   Подростки: 13–18
#   Взрослые:  19–60
#   Пожилые:   60+

AGE_GROUP_KEYS = ("kids", "teens", "adults", "elderly")


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


# ── Настройки парка ───────────────────────────────────────────────────────

def get_settings(db: Session) -> ParkSettings:
    """Singleton-строка настроек; создаётся с дефолтами при первом обращении."""
    s = db.get(ParkSettings, SETTINGS_ID)
    if s:
        return s

    s = ParkSettings(id=SETTINGS_ID, **DEFAULT_SETTINGS)
    db.add(s)
    try:
        db.commit()
        db.refresh(s)
        return s
    except IntegrityError:
        # Другая реплика успела создать singleton row одновременно.
        db.rollback()
        existing = db.get(ParkSettings, SETTINGS_ID)
        if existing is None:
            raise
        return existing


def _validate_settings(capacity: int, warning_threshold: int, tz_name: str) -> None:
    if capacity <= 0:
        raise ValueError("capacity должен быть положительным")
    if warning_threshold <= 0:
        raise ValueError("warning_threshold должен быть положительным")
    if warning_threshold > capacity:
        raise ValueError("warning_threshold не может быть больше capacity")
    try:
        ZoneInfo(tz_name)
    except Exception as exc:
        raise ValueError(f"Невалидная timezone: {tz_name}") from exc


def update_settings(db: Session, **fields) -> ParkSettings:
    """Частичное обновление: поля со значением None не меняются.

    Валидация идёт по итоговым значениям (новые поверх текущих), чтобы нельзя
    было, например, уменьшить capacity ниже уже сохранённого порога.
    """
    s = get_settings(db)
    changes = {k: v for k, v in fields.items() if v is not None}
    _validate_settings(
        changes.get("capacity", s.capacity),
        changes.get("warning_threshold", s.warning_threshold),
        changes.get("timezone", s.timezone),
    )

    for k, v in changes.items():
        setattr(s, k, v)
    s.updated_at = utcnow()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise
    db.refresh(s)
    return s


# ── Время и границы периодов ─────────────────────────────────────────────────

def _tz(db: Session) -> ZoneInfo:
    return ZoneInfo(get_settings(db).timezone)


def current_park_date(db: Session) -> date:
    return datetime.now(_tz(db)).date()


def local_midnight_to_utc(local_day: date, tz: ZoneInfo) -> datetime:
    """Начало суток (00:00) в таймзоне парка, переведённое в naive UTC
    (так как ts в БД хранится как naive UTC)."""
    local_dt = datetime.combine(local_day, time.min, tzinfo=tz)
    return local_dt.astimezone(timezone.utc).replace(tzinfo=None)


def _to_local(ts: datetime, tz: ZoneInfo) -> datetime:
    return ts.replace(tzinfo=timezone.utc).astimezone(tz)


def _week_start(any_day: date) -> date:
    """Понедельник недели, содержащей any_day."""
    return any_day - timedelta(days=any_day.weekday())


def _month_range(any_day: date) -> tuple[date, date]:
    """(первое число месяца, первое число следующего месяца)."""
    start = any_day.replace(day=1)
    if start.month == 12:
        return start, start.replace(year=start.year + 1, month=1)
    return start, start.replace(month=start.month + 1)


def _local_range_to_utc(start_local: date, end_local: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    return local_midnight_to_utc(start_local, tz), local_midnight_to_utc(end_local, tz)


def period_bounds(db: Session, period: str, any_day: date) -> tuple[datetime, datetime]:
    """Границы периода (в UTC) для демографии: day | week | month."""
    tz = _tz(db)
    if period == "week":
        start_local = _week_start(any_day)
        end_local = start_local + timedelta(days=7)
    elif period == "month":
        start_local, end_local = _month_range(any_day)
    else:  # day
        start_local, end_local = any_day, any_day + timedelta(days=1)
    return _local_range_to_utc(start_local, end_local, tz)


# ── Общие фильтры запросов ───────────────────────────────────────────────────

def _in_range(q: Query, start_utc: datetime, end_utc: datetime) -> Query:
    """Полуинтервал [start_utc, end_utc)."""
    return q.filter(CrossingEvent.ts >= start_utc, CrossingEvent.ts < end_utc)


def _by_gate(q: Query, gate: str | None) -> Query:
    return q.filter(CrossingEvent.gate == gate) if gate else q


# ── Запись события (вызывается воркером на каждое распознанное пересечение) ─

def record_crossing(db: Session, camera, person_id, age, gender, ts=None) -> CrossingEvent:
    """Сохраняет пересечение. Некорректные age (<0) и gender (не 0/1) от
    модели пишутся как NULL, чтобы не портить демографию."""
    event = CrossingEvent(
        camera_id=camera.id,
        direction=camera.direction,
        gate=camera.gate,
        person_id=person_id,
        age=age if (age is not None and age >= 0) else None,
        gender=gender if gender in (0, 1) else None,
        ts=ts or utcnow(),
    )
    db.add(event)
    db.commit()
    return event


def log_crossing(camera, person_id, age, gender, ts=None) -> bool:
    """Записывает событие в отдельной сессии и возвращает True только после
    успешного COMMIT (ошибка БД печатается, а не пробрасывается)."""
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
# распознавания "тот же человек или новый" (см. FaceStoragePG.identify).
# По требованию продукта эта галерея должна каждый день начинаться с нуля
# (в 12:00 по таймзоне парка, см. worker: persons_reset), т.е. один и тот же
# посетитель, пришедший сегодня и завтра, будет распознан как два разных Person.
#
# crossing_events (сама история пересечений — сколько, когда, откуда) не
# трогаем: только отвязываем от неё удаляемые person_id (age/gender на
# момент пересечения уже сохранены в самой строке события и не теряются).

def reset_persons(db: Session) -> int:
    """Полностью очищает таблицу persons и возвращает число удалённых строк.

    История crossing_events не удаляется — у событий только обнуляется
    person_id, чтобы не нарушить внешний ключ.
    """
    db.query(CrossingEvent).filter(CrossingEvent.person_id.isnot(None)).update(
        {CrossingEvent.person_id: None}, synchronize_session=False
    )
    deleted = db.query(Person).delete(synchronize_session=False)
    db.commit()
    return deleted


# ── Live Occupancy (раздел ТЗ "Блок Б") ──────────────────────────────────────

def _occupancy_status(occupancy: int, settings: ParkSettings) -> str:
    """ok | warning | full — для цвета виджета."""
    if occupancy >= settings.capacity:
        return "full"
    if occupancy >= settings.warning_threshold:
        return "warning"
    return "ok"


def get_live_occupancy(db: Session, gate: str | None = None) -> dict:
    settings = get_settings(db)
    tz = ZoneInfo(settings.timezone)
    start_utc = local_midnight_to_utc(datetime.now(tz).date(), tz)
    now_utc = utcnow()

    base = db.query(CrossingEvent).filter(
        CrossingEvent.ts >= start_utc,
        CrossingEvent.ts <= now_utc,
    )
    base = _by_gate(base, gate)
    total_in = base.filter(CrossingEvent.direction == "in").count()
    total_out = base.filter(CrossingEvent.direction == "out").count()

    # Защита от рассинхрона камер (например, если одна из камер временно
    # не работала) — занятость не может быть отрицательной.
    occupancy = max(total_in - total_out, 0)

    return {
        "occupancy": occupancy,
        "capacity": settings.capacity,
        "warning_threshold": settings.warning_threshold,
        "status": _occupancy_status(occupancy, settings),
        "today_in": total_in,
        "today_out": total_out,
        "reset_at_utc": start_utc.isoformat() + "Z",
        "gate": gate,
    }


# ── День: график по часам + пики (раздел ТЗ "3. Структура Дашборда") ───────

def _peak_index(values: list[int]) -> int | None:
    """Индекс первого максимума или None, если все нули."""
    if not any(values):
        return None
    return max(range(len(values)), key=lambda i: values[i])


def get_day_timeseries(db: Session, day: date, gate: str | None = None) -> dict:
    tz = _tz(db)
    start_utc, end_utc = _local_range_to_utc(day, day + timedelta(days=1), tz)

    q = db.query(CrossingEvent.direction, CrossingEvent.ts)
    q = _by_gate(_in_range(q, start_utc, end_utc), gate)

    hourly_in = [0] * 24
    hourly_out = [0] * 24
    for direction, ts in q.all():
        target = hourly_in if direction == "in" else hourly_out
        target[_to_local(ts, tz).hour] += 1

    peak_in_hour = _peak_index(hourly_in)
    peak_out_hour = _peak_index(hourly_out)

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
    week_start = _week_start(any_day)
    start_utc, end_utc = _local_range_to_utc(week_start, week_start + timedelta(days=7), tz)

    q = db.query(CrossingEvent.direction, CrossingEvent.ts)
    q = _by_gate(_in_range(q, start_utc, end_utc), gate)

    daily_in = defaultdict(int)
    daily_out = defaultdict(int)
    for direction, ts in q.all():
        target = daily_in if direction == "in" else daily_out
        target[_to_local(ts, tz).date()] += 1

    daily = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        daily.append({
            "date": d.isoformat(),
            "weekday": d.isoweekday(),  # 1=Пн .. 7=Вс
            "in": daily_in.get(d, 0),
            "out": daily_out.get(d, 0),
        })

    def busiest(metric) -> str | None:
        if not any(metric(r) for r in daily):
            return None
        return max(daily, key=metric)["date"]

    return {
        "week_start": week_start.isoformat(),
        "gate": gate,
        "daily": daily,
        "busiest_in_day": busiest(lambda r: r["in"]),
        "busiest_total_day": busiest(lambda r: r["in"] + r["out"]),
    }


# ── Месяц: накопленный итог уникальных гостей (раздел ТЗ "3. ... Месяц") ───

def get_month_timeseries(db: Session, any_day: date, gate: str | None = None) -> dict:
    tz = _tz(db)
    month_start, next_month_start = _month_range(any_day)
    start_utc, end_utc = _local_range_to_utc(month_start, next_month_start, tz)

    q = db.query(CrossingEvent.person_id, CrossingEvent.ts).filter(
        CrossingEvent.direction == "in",
        CrossingEvent.person_id.isnot(None),
    )
    q = _by_gate(_in_range(q, start_utc, end_utc), gate)

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
    today_local = datetime.now(tz).date()
    if (month_start.year, month_start.month) == (today_local.year, today_local.month):
        last_day = today_local
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
    q = db.query(CrossingEvent.age, CrossingEvent.gender).filter(CrossingEvent.direction == "in")
    rows = _by_gate(_in_range(q, start_utc, end_utc), gate).all()

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
