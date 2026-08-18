"""
employee_sync.py — синхронизация сотрудников с внешней HR-системой.

При старте воркера (и по расписанию) делает два запроса:
  1. GET /api/v1/employees          — список сотрудников (id, firstname, file)
  2. GET /api/v1/files/{file_id}/view — фото сотрудника

Для каждого полученного фото:
  - Извлекает embedding через face_engine
  - Сохраняет в таблицу employees (INSERT или UPDATE если external_id уже есть)

Сотрудники с file=0 (нет фото) пропускаются.
"""

import io
import logging
import os
import time

import numpy as np
import httpx
from PIL import Image
import cv2

from db.database import SessionLocal
from db.models import Employee
from face_engine import extract_faces
from face_storage_pg import FaceStoragePG

log = logging.getLogger("employee_sync")

HR_BASE = os.environ.get("HR_API_BASE", "https://192.168.0.146:4050").rstrip("/")
EMPLOYEES_URL = f"{HR_BASE}/api/v1/employees"
FILE_URL_TPL = f"{HR_BASE}/api/v1/files/{{file_id}}/view"

# Сколько сотрудников запрашивать за одну страницу (HR API отдаёт постранично)
EMPLOYEES_PAGE_LIMIT = int(os.environ.get("HR_EMPLOYEES_PAGE_LIMIT", "100"))

# Отключаем проверку SSL для self-signed серта (локальная сеть)
SSL_VERIFY = os.environ.get("HR_SSL_VERIFY", "true").strip().lower() not in {"false", "0", "no"}


def _fetch_employees() -> list[dict]:
    """
    Возвращает список ВСЕХ сотрудников из HR API, проходя по всем страницам.

    Формат ответа HR API:
    {
      "statusCode": 200,
      "data": {
        "employees": [...],
        "pagination": {"total": 11, "page": 1, "limit": 100, "totalPages": 1}
      }
    }
    """
    all_employees: list[dict] = []
    page = 1

    while True:
        try:
            resp = httpx.get(
                EMPLOYEES_URL,
                params={"page": page, "limit": EMPLOYEES_PAGE_LIMIT},
                verify=SSL_VERIFY,
                timeout=10,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            log.error(f"[employee_sync] Не удалось получить страницу {page} списка сотрудников: {e}")
            break

        data = payload.get("data", {})
        employees = data.get("employees", [])
        all_employees.extend(employees)

        pagination = data.get("pagination", {})
        total_pages = pagination.get("totalPages", 1)

        log.info(
            f"[employee_sync] Страница {page}/{total_pages}: получено {len(employees)} сотрудников "
            f"(всего собрано: {len(all_employees)})"
        )

        if not employees or page >= total_pages:
            break

        page += 1

    return all_employees


def _fetch_photo_as_frame(file_id: int) -> np.ndarray | None:
    """Скачивает фото сотрудника и возвращает BGR numpy array (cv2 frame)."""
    url = FILE_URL_TPL.format(file_id=file_id)
    try:
        resp = httpx.get(url, verify=SSL_VERIFY, timeout=15)
        resp.raise_for_status()
        img_array = np.frombuffer(resp.content, dtype=np.uint8)
        frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
        if frame is None:
            log.warning(f"[employee_sync] file_id={file_id}: не удалось декодировать изображение")
        return frame
    except Exception as e:
        log.error(f"[employee_sync] Не удалось скачать фото file_id={file_id}: {e}")
        return None


def _emb_to_bytes(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def _upsert_employee(
    session,
    external_id: int,
    first_name: str,
    file_id: int,
    embedding: np.ndarray,
) -> bool:
    """
    INSERT или UPDATE сотрудника.
    Возвращает True если добавлен новый, False если обновлён существующий.
    """
    existing = (
        session.query(Employee)
        .filter(Employee.external_id == external_id)
        .first()
    )
    if existing:
        existing.embedding = _emb_to_bytes(embedding)
        existing.first_name = first_name
        existing.file_id = file_id
        return False
    else:
        emp = Employee(
            external_id=external_id,
            first_name=first_name,
            file_id=file_id,
            embedding=_emb_to_bytes(embedding),
        )
        session.add(emp)
        return True



def add_employee_from_file(first_name: str, file_id: int, external_id: int | None = None) -> dict:
    """Создаёт/обновляет сотрудника по фото HR API в worker-контейнере."""
    frame = _fetch_photo_as_frame(file_id)
    if frame is None:
        raise RuntimeError(f"Не удалось скачать или декодировать фото file_id={file_id}")

    faces = extract_faces(frame)
    if not faces:
        raise ValueError("Лицо не найдено на фотографии")
    face = faces[0]
    if face.embedding is None:
        raise ValueError("Не удалось извлечь embedding лица")

    session = SessionLocal()
    try:
        if external_id is not None:
            existing = session.query(Employee).filter(Employee.external_id == external_id).first()
            if existing is not None:
                existing.embedding = _emb_to_bytes(face.embedding)
                existing.first_name = first_name
                existing.file_id = file_id
                session.commit()
                session.refresh(existing)
                FaceStoragePG.invalidate_cache()
                return {
                    "id": existing.id, "external_id": existing.external_id,
                    "first_name": existing.first_name, "file_id": existing.file_id,
                    "created_at": existing.created_at.isoformat() if existing.created_at else None,
                }

        emp = Employee(
            external_id=external_id,
            first_name=first_name,
            file_id=file_id,
            embedding=_emb_to_bytes(face.embedding),
        )
        session.add(emp)
        try:
            session.commit()
        except Exception:
            session.rollback()
            # Параллельный запрос мог вставить тот же external_id.
            if external_id is not None:
                existing = session.query(Employee).filter(Employee.external_id == external_id).first()
                if existing is not None:
                    existing.embedding = _emb_to_bytes(face.embedding)
                    existing.first_name = first_name
                    existing.file_id = file_id
                    session.commit()
                    session.refresh(existing)
                    FaceStoragePG.invalidate_cache()
                    return {
                        "id": existing.id, "external_id": existing.external_id,
                        "first_name": existing.first_name, "file_id": existing.file_id,
                        "created_at": existing.created_at.isoformat() if existing.created_at else None,
                    }
            raise
        session.refresh(emp)
        result = {
            "id": emp.id, "external_id": emp.external_id,
            "first_name": emp.first_name, "file_id": emp.file_id,
            "created_at": emp.created_at.isoformat() if emp.created_at else None,
        }
        FaceStoragePG.invalidate_cache()
        return result
    finally:
        session.close()

def sync_employees() -> dict:
    """
    Основная функция синхронизации.
    Вызывается при старте и может вызываться повторно по расписанию.
    Возвращает {"added": N, "updated": N, "skipped": N, "errors": N}
    """
    log.info("[employee_sync] Начало синхронизации сотрудников...")
    employees = _fetch_employees()

    if not employees:
        log.warning("[employee_sync] Список сотрудников пуст или недоступен")
        return {"added": 0, "updated": 0, "skipped": 0, "errors": 0}

    added = updated = skipped = errors = 0

    for emp in employees:
        ext_id = emp.get("id")
        first_name = emp.get("firstname", "Unknown")
        file_id = emp.get("file", 0)

        if not file_id:
            log.info(f"[employee_sync] Сотрудник id={ext_id} ({first_name}): нет фото, пропускаем")
            skipped += 1
            continue

        # Скачиваем фото
        frame = _fetch_photo_as_frame(file_id)
        if frame is None:
            errors += 1
            continue

        # Извлекаем вектор лица
        faces = extract_faces(frame)
        if not faces:
            log.warning(
                f"[employee_sync] Сотрудник id={ext_id} ({first_name}): "
                f"лицо не найдено на фото file_id={file_id}"
            )
            skipped += 1
            continue

        # Берём первое (и обычно единственное) лицо на фото сотрудника
        face = faces[0]
        if face.embedding is None:
            log.warning(f"[employee_sync] Сотрудник id={ext_id}: embedding=None")
            skipped += 1
            continue

        session = SessionLocal()
        try:
            is_new = _upsert_employee(session, ext_id, first_name, file_id, face.embedding)
            session.commit()
            if is_new:
                added += 1
                log.info(f"[employee_sync] Добавлен: {first_name} (external_id={ext_id})")
            else:
                updated += 1
                log.info(f"[employee_sync] Обновлён: {first_name} (external_id={ext_id})")
        except Exception as e:
            session.rollback()
            # При параллельном старте двух worker-реплик обе могут увидеть
            # отсутствие external_id. Если одна уже вставила строку, вторая
            # повторно получает её и делает UPDATE вместо ложной ошибки.
            try:
                existing = (
                    session.query(Employee)
                    .filter(Employee.external_id == ext_id)
                    .first()
                )
                if existing is not None:
                    existing.embedding = _emb_to_bytes(face.embedding)
                    existing.first_name = first_name
                    existing.file_id = file_id
                    session.commit()
                    updated += 1
                    log.info(f"[employee_sync] Гонка INSERT разрешена UPDATE: {first_name} (external_id={ext_id})")
                else:
                    raise e
            except Exception:
                session.rollback()
                log.error(f"[employee_sync] Ошибка сохранения сотрудника id={ext_id}: {e}")
                errors += 1
        finally:
            session.close()

    FaceStoragePG.invalidate_cache()
    result = {"added": added, "updated": updated, "skipped": skipped, "errors": errors}
    log.info(f"[employee_sync] Завершено: {result}")
    return result
