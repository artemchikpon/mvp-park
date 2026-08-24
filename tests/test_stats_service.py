"""
Тесты для shared/db/stats_service.py — бизнес-логики аналитики (Live Occupancy,
графики по дням/неделям/месяцам, демография). Это самая ценная для тестов
часть проекта: чистые вычисления над БД, без сети/камер/ML.
"""

from datetime import date, datetime, timedelta

import pytest

from db import stats_service as svc
from db.models import Camera, CrossingEvent, Person


# ── group_age ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "age,expected",
    [
        (0, "kids"),
        (12, "kids"),
        (13, "teens"),
        (18, "teens"),
        (19, "adults"),
        (60, "adults"),
        (61, "elderly"),
        (120, "elderly"),
        (None, None),
        (-1, None),  # некорректный возраст (модель ошиблась) -> не группируем
    ],
)
def test_group_age_boundaries(age, expected):
    assert svc.group_age(age) == expected


# ── settings ─────────────────────────────────────────────────────────────────

def test_get_settings_creates_default_row_once(db_session):
    s1 = svc.get_settings(db_session)
    assert s1.id == 1
    assert s1.capacity == 5000
    assert s1.warning_threshold == 4500
    assert s1.timezone == "UTC"

    # повторный вызов не должен плодить новую строку
    s2 = svc.get_settings(db_session)
    assert s2.id == s1.id
    assert db_session.query(svc.ParkSettings).count() == 1


def test_update_settings_only_changes_provided_fields(db_session):
    svc.get_settings(db_session)  # создать дефолт
    updated = svc.update_settings(db_session, capacity=5000, warning_threshold=None, timezone=None)
    assert updated.capacity == 5000
    # None-поля не переданы -> не меняются.
    assert updated.warning_threshold == 4500
    assert updated.timezone == "UTC"


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_camera(db_session, direction="in", gate="Центральный", name="Cam"):
    cam = Camera(name=name, url="rtsp://example/1", direction=direction, gate=gate)
    db_session.add(cam)
    db_session.commit()
    db_session.refresh(cam)
    return cam


def _add_event(db_session, camera, ts_utc, person_id=1, age=None, gender=None):
    ev = CrossingEvent(
        camera_id=camera.id,
        direction=camera.direction,
        gate=camera.gate,
        person_id=person_id,
        age=age,
        gender=gender,
        ts=ts_utc,
    )
    db_session.add(ev)
    db_session.commit()
    return ev


# ── live occupancy ───────────────────────────────────────────────────────────

def test_live_occupancy_basic_in_out_balance(db_session):
    svc.update_settings(db_session, capacity=100, warning_threshold=80)
    cam_in = _make_camera(db_session, direction="in", name="In")
    cam_out = _make_camera(db_session, direction="out", name="Out")

    now = datetime.utcnow()
    for i in range(5):
        _add_event(db_session, cam_in, now, person_id=i)
    for i in range(2):
        _add_event(db_session, cam_out, now, person_id=i)

    result = svc.get_live_occupancy(db_session)
    assert result["today_in"] == 5
    assert result["today_out"] == 2
    assert result["occupancy"] == 3
    assert result["status"] == "ok"


def test_live_occupancy_never_negative_when_out_exceeds_in(db_session):
    """Защита от рассинхрона камер: occupancy не должен уходить в минус,
    даже если out > in (например, одна из камер входа была офлайн)."""
    svc.get_settings(db_session)
    cam_out = _make_camera(db_session, direction="out")
    now = datetime.utcnow()
    for i in range(3):
        _add_event(db_session, cam_out, now, person_id=i)

    result = svc.get_live_occupancy(db_session)
    assert result["occupancy"] == 0


def test_live_occupancy_status_thresholds(db_session):
    svc.update_settings(db_session, capacity=10, warning_threshold=8)
    cam_in = _make_camera(db_session, direction="in")
    now = datetime.utcnow()

    for i in range(8):
        _add_event(db_session, cam_in, now, person_id=i)
    assert svc.get_live_occupancy(db_session)["status"] == "warning"

    for i in range(8, 10):
        _add_event(db_session, cam_in, now, person_id=i)
    assert svc.get_live_occupancy(db_session)["status"] == "full"


def test_live_occupancy_only_counts_today(db_session):
    svc.get_settings(db_session)
    cam_in = _make_camera(db_session, direction="in")
    yesterday = datetime.utcnow() - timedelta(days=1, hours=1)
    _add_event(db_session, cam_in, yesterday, person_id=1)

    result = svc.get_live_occupancy(db_session)
    assert result["today_in"] == 0
    assert result["occupancy"] == 0


def test_live_occupancy_filters_by_gate(db_session):
    svc.get_settings(db_session)
    cam_a = _make_camera(db_session, direction="in", gate="A")
    cam_b = _make_camera(db_session, direction="in", gate="B")
    now = datetime.utcnow()
    _add_event(db_session, cam_a, now, person_id=1)
    _add_event(db_session, cam_a, now, person_id=2)
    _add_event(db_session, cam_b, now, person_id=3)

    result = svc.get_live_occupancy(db_session, gate="A")
    assert result["today_in"] == 2
    assert result["gate"] == "A"


# ── day timeseries ───────────────────────────────────────────────────────────

def test_day_timeseries_buckets_by_utc_hour_when_tz_is_utc(db_session):
    svc.get_settings(db_session)  # timezone=UTC по умолчанию
    cam_in = _make_camera(db_session, direction="in")
    today = date.today()
    ts_9am = datetime(today.year, today.month, today.day, 9, 15)
    ts_9am_2 = datetime(today.year, today.month, today.day, 9, 45)
    ts_2pm = datetime(today.year, today.month, today.day, 14, 0)

    _add_event(db_session, cam_in, ts_9am, person_id=1)
    _add_event(db_session, cam_in, ts_9am_2, person_id=2)
    _add_event(db_session, cam_in, ts_2pm, person_id=3)

    result = svc.get_day_timeseries(db_session, today)
    assert result["hourly"][9]["in"] == 2
    assert result["hourly"][14]["in"] == 1
    assert result["total_in"] == 3
    assert result["peak_in_hour"] == 9


def test_day_timeseries_empty_day_has_no_peak(db_session):
    svc.get_settings(db_session)
    result = svc.get_day_timeseries(db_session, date.today())
    assert result["peak_in_hour"] is None
    assert result["peak_out_hour"] is None
    assert result["estimated_session_hours"] is None
    assert result["total_in"] == 0


def test_day_timeseries_session_hours_only_when_out_after_in(db_session):
    svc.get_settings(db_session)
    cam_in = _make_camera(db_session, direction="in")
    cam_out = _make_camera(db_session, direction="out")
    today = date.today()

    # Пик входа в 9:00, пик выхода в 18:00 -> оценка длины сессии = 9ч
    for _ in range(3):
        _add_event(db_session, cam_in, datetime(today.year, today.month, today.day, 9, 0), person_id=1)
    for _ in range(3):
        _add_event(db_session, cam_out, datetime(today.year, today.month, today.day, 18, 0), person_id=1)

    result = svc.get_day_timeseries(db_session, today)
    assert result["peak_in_hour"] == 9
    assert result["peak_out_hour"] == 18
    assert result["estimated_session_hours"] == 9


def test_day_timeseries_no_session_estimate_when_out_before_in(db_session):
    svc.get_settings(db_session)
    cam_in = _make_camera(db_session, direction="in")
    cam_out = _make_camera(db_session, direction="out")
    today = date.today()

    _add_event(db_session, cam_in, datetime(today.year, today.month, today.day, 18, 0), person_id=1)
    _add_event(db_session, cam_out, datetime(today.year, today.month, today.day, 9, 0), person_id=1)

    result = svc.get_day_timeseries(db_session, today)
    assert result["estimated_session_hours"] is None


# ── week timeseries ──────────────────────────────────────────────────────────

def test_week_timeseries_groups_by_weekday_and_finds_busiest_day(db_session):
    svc.get_settings(db_session)
    cam_in = _make_camera(db_session, direction="in")
    monday = date.today() - timedelta(days=date.today().weekday())

    # Понедельник: 2 входа, среда: 5 входов
    _add_event(db_session, cam_in, datetime.combine(monday, datetime.min.time()) + timedelta(hours=10), person_id=1)
    _add_event(db_session, cam_in, datetime.combine(monday, datetime.min.time()) + timedelta(hours=11), person_id=2)
    wednesday = monday + timedelta(days=2)
    for i in range(5):
        _add_event(
            db_session, cam_in,
            datetime.combine(wednesday, datetime.min.time()) + timedelta(hours=10, minutes=i),
            person_id=10 + i,
        )

    result = svc.get_week_timeseries(db_session, monday)
    assert result["week_start"] == monday.isoformat()
    mon_row = next(r for r in result["daily"] if r["date"] == monday.isoformat())
    wed_row = next(r for r in result["daily"] if r["date"] == wednesday.isoformat())
    assert mon_row["in"] == 2
    assert wed_row["in"] == 5
    assert result["busiest_in_day"] == wednesday.isoformat()


def test_week_timeseries_any_day_resolves_to_containing_week(db_session):
    svc.get_settings(db_session)
    # Пятница той же недели должна дать тот же week_start, что и понедельник
    monday = date.today() - timedelta(days=date.today().weekday())
    friday = monday + timedelta(days=4)
    result_from_friday = svc.get_week_timeseries(db_session, friday)
    assert result_from_friday["week_start"] == monday.isoformat()


# ── month timeseries (уникальные гости) ────────────────────────────────────

def test_month_timeseries_counts_unique_persons_by_first_visit(db_session):
    svc.get_settings(db_session)
    cam_in = _make_camera(db_session, direction="in")
    month_start = date.today().replace(day=1)
    day1 = datetime.combine(month_start, datetime.min.time()) + timedelta(hours=10)
    day2 = day1 + timedelta(days=1)

    # person 1 приходит в day1 и снова в day2 -> считается уникальным только
    # в day1 (по первому визиту в месяце)
    _add_event(db_session, cam_in, day1, person_id=1)
    _add_event(db_session, cam_in, day2, person_id=1)
    # person 2 приходит только в day2
    _add_event(db_session, cam_in, day2, person_id=2)

    result = svc.get_month_timeseries(db_session, day2.date())
    by_date = {r["date"]: r for r in result["daily"]}
    assert by_date[day1.date().isoformat()]["new_unique"] == 1
    assert by_date[day2.date().isoformat()]["new_unique"] == 1
    assert by_date[day2.date().isoformat()]["cumulative_unique"] == 2
    assert result["total_unique"] == 2


def test_month_timeseries_ignores_out_direction_and_null_person(db_session):
    svc.get_settings(db_session)
    cam_in = _make_camera(db_session, direction="in")
    cam_out = _make_camera(db_session, direction="out")
    today = datetime.utcnow()

    _add_event(db_session, cam_out, today, person_id=1)  # direction=out -> не считается
    _add_event(db_session, cam_in, today, person_id=None)  # person_id=None -> не считается

    result = svc.get_month_timeseries(db_session, today.date())
    assert result["total_unique"] == 0


# ── demographics ─────────────────────────────────────────────────────────────

def test_demographics_gender_and_age_group_breakdown(db_session):
    svc.get_settings(db_session)
    cam_in = _make_camera(db_session, direction="in")
    now = datetime.utcnow()

    _add_event(db_session, cam_in, now, person_id=1, age=10, gender=1)   # kid, male
    _add_event(db_session, cam_in, now, person_id=2, age=30, gender=0)   # adult, female
    _add_event(db_session, cam_in, now, person_id=3, age=70, gender=1)   # elderly, male
    _add_event(db_session, cam_in, now, person_id=4, age=None, gender=None)  # неизвестно

    start, end = svc.period_bounds(db_session, "day", now.date())
    result = svc.get_demographics(db_session, start, end)

    assert result["total_in"] == 4
    assert result["male"] == 2
    assert result["female"] == 1
    assert result["age_groups"]["kids"] == 1
    assert result["age_groups"]["adults"] == 1
    assert result["age_groups"]["elderly"] == 1
    assert result["male_pct"] == pytest.approx(50.0)


def test_demographics_excludes_out_direction(db_session):
    svc.get_settings(db_session)
    cam_out = _make_camera(db_session, direction="out")
    now = datetime.utcnow()
    _add_event(db_session, cam_out, now, person_id=1, age=25, gender=1)

    start, end = svc.period_bounds(db_session, "day", now.date())
    result = svc.get_demographics(db_session, start, end)
    assert result["total_in"] == 0


def test_demographics_percentages_are_zero_when_no_data(db_session):
    svc.get_settings(db_session)
    start, end = svc.period_bounds(db_session, "day", date.today())
    result = svc.get_demographics(db_session, start, end)
    assert result["male_pct"] == 0.0
    assert result["female_pct"] == 0.0


# ── record_crossing / log_crossing: санитизация некорректных значений ──────

def test_record_crossing_sanitizes_invalid_age_and_gender(db_session):
    cam = _make_camera(db_session, direction="in")
    event = svc.record_crossing(db_session, cam, person_id=1, age=-5, gender=2)
    assert event.age is None       # отрицательный возраст -> None
    assert event.gender is None    # gender не 0/1 -> None


def test_record_crossing_keeps_valid_zero_age_and_gender(db_session):
    """0 — валидные значения (новорождённый; gender=0=female), их нельзя
    случайно занулить через `if age` / `if gender` (там именно `is not None`)."""
    cam = _make_camera(db_session, direction="in")
    event = svc.record_crossing(db_session, cam, person_id=1, age=0, gender=0)
    assert event.age == 0
    assert event.gender == 0


# ── reset_persons (ежедневный сброс галереи лиц) ────────────────────────────

def _make_person(db_session, embedding=b"\x00" * 8):
    p = Person(embedding=embedding, age=30, gender=1)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def test_reset_persons_deletes_all_rows_and_returns_count(db_session):
    _make_person(db_session)
    _make_person(db_session)
    _make_person(db_session)

    deleted = svc.reset_persons(db_session)

    assert deleted == 3
    assert db_session.query(Person).count() == 0


def test_reset_persons_nulls_person_id_on_existing_crossing_events(db_session):
    """Историю посещений (crossing_events) сброс не должен удалять — только
    отвязать её от больше не существующих Person, иначе FK был бы нарушен."""
    cam = _make_camera(db_session, direction="in")
    person = _make_person(db_session)
    event = _add_event(db_session, cam, datetime.utcnow(), person_id=person.id)

    svc.reset_persons(db_session)

    db_session.refresh(event)
    assert event.person_id is None
    # само событие (и его age/gender/ts на момент пересечения) сохраняется
    assert db_session.query(CrossingEvent).count() == 1


def test_reset_persons_returns_zero_when_table_already_empty(db_session):
    assert svc.reset_persons(db_session) == 0
