#!/usr/bin/env python3
"""
Заполняет БД тестовыми данными для дашбордов MVP Park.

Что создаётся:
  - Настройки парка (ParkSettings)
  - 6 камер (3 входа, 3 выхода, 3 точки)
  - 500 уникальных персон (Person) с возрастом и полом
  - ~15 000 событий CrossingEvent за последние 30 дней
    с реалистичным суточным профилем посещаемости

Запуск из корня репозитория (Postgres из docker-compose проброшен на localhost):
  DATABASE_URL=postgresql://face:face@localhost:5432/face_db python scripts/seed_dashboard.py
"""

import os
import random
import struct
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

# Скрипт запускается вне контейнеров: подключаем shared/ (пакеты db, core)
# и по умолчанию смотрим на локальный Postgres.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))
os.environ.setdefault("DATABASE_URL", "postgresql://face:face@localhost:5432/face_db")

from sqlalchemy import text  # noqa: E402

from core.timeutil import utcnow  # noqa: E402
from db.database import SessionLocal  # noqa: E402
from db.models import Camera, CrossingEvent, ParkSettings, Person  # noqa: E402


# ── Вспомогательные функции ────────────────────────────────────────────────────

def fake_embedding(dim: int = 128) -> bytes:
    """Случайный float32-вектор как bytes."""
    return struct.pack(f"{dim}f", *[random.gauss(0, 1) for _ in range(dim)])


def hourly_weight(hour: int) -> float:
    """Профиль посещаемости по часам (парк, пик утром и вечером)."""
    profile = {
        6: 0.2, 7: 0.5, 8: 1.0, 9: 1.5, 10: 2.0,
        11: 2.5, 12: 2.8, 13: 2.5, 14: 2.2, 15: 2.0,
        16: 2.3, 17: 2.8, 18: 3.0, 19: 2.5, 20: 1.8,
        21: 1.0, 22: 0.4, 23: 0.1,
    }
    return profile.get(hour, 0.05)


def random_ts_in_day(day: date) -> datetime:
    """Случайный timestamp за день с весом по часу."""
    # Генерируем час по профилю
    hours = list(range(6, 24))
    weights = [hourly_weight(h) for h in hours]
    hour = random.choices(hours, weights=weights, k=1)[0]
    minute = random.randint(0, 59)
    second = random.randint(0, 59)
    return datetime.combine(day, time(hour, minute, second))


def weighted_age() -> int:
    """Возраст с реалистичным распределением для парка."""
    groups = ["kids", "teens", "adults", "elderly"]
    weights = [0.15, 0.10, 0.65, 0.10]
    group = random.choices(groups, weights=weights, k=1)[0]
    if group == "kids":
        return random.randint(2, 12)
    elif group == "teens":
        return random.randint(13, 18)
    elif group == "adults":
        return random.randint(19, 60)
    else:
        return random.randint(61, 85)


# ── Основной сидер ─────────────────────────────────────────────────────────────

def seed():
    db = SessionLocal()

    try:
        # ── 1. Настройки парка ────────────────────────────────────────────────
        print("→ ParkSettings...")
        existing = db.get(ParkSettings, 1)
        if not existing:
            db.add(ParkSettings(
                id=1,
                capacity=5000,
                warning_threshold=4500,
                timezone="Asia/Tashkent",
                updated_at=utcnow(),
            ))
            db.commit()
            print("  Создано.")
        else:
            print("  Уже есть, пропускаем.")

        # ── 2. Камеры ─────────────────────────────────────────────────────────
        print("→ Камеры...")
        cameras_data = [
            # Центральный вход
            ("Центральный вход — IN",  "rtsp://cam1.park.local/stream", "in",  "Центральный"),
            ("Центральный выход — OUT", "rtsp://cam2.park.local/stream", "out", "Центральный"),
            # Северный
            ("Северный вход — IN",     "rtsp://cam3.park.local/stream", "in",  "Северный"),
            ("Северный выход — OUT",   "rtsp://cam4.park.local/stream", "out", "Северный"),
            # Второй (МОРЕ)
            ("МОРЕ вход — IN",         "rtsp://cam5.park.local/stream", "in",  "Второй (МОРЕ)"),
            ("МОРЕ выход — OUT",       "rtsp://cam6.park.local/stream", "out", "Второй (МОРЕ)"),
        ]

        existing_cameras = db.query(Camera).all()
        if existing_cameras:
            cameras = existing_cameras
            print(f"  Найдено {len(cameras)} камер, используем их.")
        else:
            cameras = []
            for name, url, direction, gate in cameras_data:
                c = Camera(name=name, url=url, direction=direction, gate=gate)
                db.add(c)
                cameras.append(c)
            db.commit()
            for c in cameras:
                db.refresh(c)
            print(f"  Создано {len(cameras)} камер.")

        in_cameras  = [c for c in cameras if c.direction == "in"]
        out_cameras = [c for c in cameras if c.direction == "out"]

        # ── 3. Персоны ────────────────────────────────────────────────────────
        NUM_PERSONS = 500
        print(f"→ Персоны ({NUM_PERSONS})...")

        existing_count = db.query(Person).count()
        if existing_count >= NUM_PERSONS:
            persons = db.query(Person).limit(NUM_PERSONS).all()
            print(f"  Найдено {existing_count} персон, используем первые {NUM_PERSONS}.")
        else:
            persons = db.query(Person).all()
            to_create = NUM_PERSONS - len(persons)
            base_dt = utcnow() - timedelta(days=60)
            for _ in range(to_create):
                age = weighted_age()
                gender = random.choice([0, 1])
                first = base_dt + timedelta(days=random.randint(0, 59))
                p = Person(
                    embedding=fake_embedding(128),
                    age=age,
                    gender=gender,
                    first_seen=first,
                    last_seen=first,
                )
                db.add(p)
                persons.append(p)
            db.commit()
            for p in persons:
                if p.id is None:
                    db.refresh(p)
            print(f"  Создано {to_create} новых персон (итого {len(persons)}).")

        # ── 4. События CrossingEvent ──────────────────────────────────────────
        DAYS_BACK = 30
        print(f"→ События CrossingEvent за {DAYS_BACK} дней...")

        today = date.today()
        start_date = today - timedelta(days=DAYS_BACK - 1)

        # Примерное количество событий в день (вход+выход) варьируется
        # В будни меньше, в выходные больше
        events_batch = []
        total_events = 0

        for d in range(DAYS_BACK):
            current_day = start_date + timedelta(days=d)
            weekday = current_day.weekday()  # 0=пн, 6=вс

            # Выходные в 1.5x больше
            base_visits = 300 if weekday >= 5 else 200
            # Небольшая случайность
            num_visits = int(base_visits * random.uniform(0.8, 1.2))

            for _ in range(num_visits):
                person = random.choice(persons)
                in_cam = random.choice(in_cameras)
                ts_in = random_ts_in_day(current_day)

                # Событие входа
                events_batch.append(CrossingEvent(
                    camera_id=in_cam.id,
                    direction="in",
                    gate=in_cam.gate,
                    person_id=person.id,
                    age=person.age,
                    gender=person.gender,
                    ts=ts_in,
                ))

                # Событие выхода — через 0.5–4 часа, с вероятностью 90%
                if random.random() < 0.90:
                    stay_minutes = random.randint(30, 240)
                    ts_out = ts_in + timedelta(minutes=stay_minutes)
                    # Не выходим за полночь
                    if ts_out.date() == current_day:
                        out_cam = random.choice(out_cameras)
                        events_batch.append(CrossingEvent(
                            camera_id=out_cam.id,
                            direction="out",
                            gate=out_cam.gate,
                            person_id=person.id,
                            age=person.age,
                            gender=person.gender,
                            ts=ts_out,
                        ))

            # Записываем пачками по 1000
            if len(events_batch) >= 1000:
                db.bulk_save_objects(events_batch)
                db.commit()
                total_events += len(events_batch)
                print(f"  День {current_day}: сохранено {total_events} событий...")
                events_batch = []

        if events_batch:
            db.bulk_save_objects(events_batch)
            db.commit()
            total_events += len(events_batch)

        print(f"  Итого создано событий: {total_events}")

        # ── Итог ──────────────────────────────────────────────────────────────
        ev_count = db.execute(text("SELECT COUNT(*) FROM crossing_events")).scalar()
        p_count  = db.execute(text("SELECT COUNT(*) FROM persons")).scalar()
        c_count  = db.execute(text("SELECT COUNT(*) FROM cameras")).scalar()

        print("\n✅ Готово!")
        print(f"   cameras:         {c_count}")
        print(f"   persons:         {p_count}")
        print(f"   crossing_events: {ev_count}")

    finally:
        db.close()


if __name__ == "__main__":
    seed()