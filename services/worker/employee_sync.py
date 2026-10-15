"""
Синхронизация сотрудников с внешней HR-системой.

HR API:
  1. GET /api/v1/employees?page=&limit=  — список сотрудников (id, firstname, file)
  2. GET /api/v1/files/{file_id}/view    — фото сотрудника

Для каждого фото извлекается embedding (face_engine) и сохраняется в таблицу
employees (INSERT или UPDATE по external_id). Сотрудники с file=0 (нет фото)
пропускаются.

Вызывается воркером при старте и по задачам из api (см. employee_jobs.py).
"""

import logging

import cv2
import httpx
import numpy as np

from db.database import SessionLocal
from db.models import Employee
from face_engine import extract_faces
from worker_config import HR_API_BASE, HR_EMPLOYEES_PAGE_LIMIT, HR_SSL_VERIFY

log = logging.getLogger("employee_sync")

EMPLOYEES_URL = f"{HR_API_BASE}/api/v1/employees"
FILE_URL_TPL = f"{HR_API_BASE}/api/v1/files/{{file_id}}/view"


class PhotoUnavailableError(RuntimeError):
    """Фото не скачалось или не декодируется."""


class FaceNotFoundError(ValueError):
    """На фото нет лица."""


class EmbeddingMissingError(FaceNotFoundError):
    """Лицо найдено, но embedding не получен."""


# ── HR API ───────────────────────────────────────────────────────────────────

def _fetch_employees() -> list[dict]:
    """
    Возвращает список ВСЕХ сотрудников из HR API, проходя по всем страницам.
    При ошибке на какой-то странице возвращает то, что успели собрать.

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
                params={"page": page, "limit": HR_EMPLOYEES_PAGE_LIMIT},
                verify=HR_SSL_VERIFY,
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
        total_pages = data.get("pagination", {}).get("totalPages", 1)

        log.info(
            f"[employee_sync] Страница {page}/{total_pages}: получено {len(employees)} сотрудников "
            f"(всего собрано: {len(all_employees)})"
        )

        if not employees or page >= total_pages:
            break
        page += 1

    return all_employees


def _fetch_photo_as_frame(file_id: int) -> np.ndarray | None:
    """Скачивает фото сотрудника и возвращает BGR-кадр (или None при ошибке)."""
    url = FILE_URL_TPL.format(file_id=file_id)
    try:
        resp = httpx.get(url, verify=HR_SSL_VERIFY, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"[employee_sync] Не удалось скачать фото file_id={file_id}: {e}")
        return None

    frame = cv2.imdecode(np.frombuffer(resp.content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        log.warning(f"[employee_sync] file_id={file_id}: не удалось декодировать изображение")
    return frame


def _embedding_from_photo(file_id: int) -> np.ndarray:
    """Embedding первого (обычно единственного) лица на фото сотрудника."""
    frame = _fetch_photo_as_frame(file_id)
    if frame is None:
        raise PhotoUnavailableError(f"Не удалось скачать или декодировать фото file_id={file_id}")

    faces = extract_faces(frame)
    if not faces:
        raise FaceNotFoundError("Лицо не найдено на фотографии")
    if faces[0].embedding is None:
        raise EmbeddingMissingError("Не удалось извлечь embedding лица")
    return faces[0].embedding


# ── БД ───────────────────────────────────────────────────────────────────────

def _find_by_external_id(session, external_id: int | None) -> Employee | None:
    if external_id is None:
        return None
    return session.query(Employee).filter(Employee.external_id == external_id).first()


def _apply(emp: Employee, first_name: str, file_id: int, embedding: np.ndarray) -> None:
    emp.first_name = first_name
    emp.file_id = file_id
    emp.embedding = np.asarray(embedding, dtype=np.float32)


def _employee_to_dict(emp: Employee) -> dict:
    return {
        "id": emp.id,
        "external_id": emp.external_id,
        "first_name": emp.first_name,
        "file_id": emp.file_id,
        "created_at": emp.created_at.isoformat() if emp.created_at else None,
    }


ADDED, UPDATED, RACE_UPDATED = "added", "updated", "race_updated"


def _save_employee(external_id: int | None, first_name: str, file_id: int,
                   embedding: np.ndarray) -> tuple[dict, str]:
    """INSERT или UPDATE сотрудника по external_id.

    Возвращает (сотрудник как dict, ADDED | UPDATED | RACE_UPDATED). При гонке
    двух реплик (обе не нашли external_id, одна успела вставить) вторая
    получает ошибку уникальности и повторяет как UPDATE (RACE_UPDATED).
    """
    session = SessionLocal()
    try:
        emp = _find_by_external_id(session, external_id)
        status = ADDED if emp is None else UPDATED
        if emp is None:
            emp = Employee(external_id=external_id)
            session.add(emp)
        _apply(emp, first_name, file_id, embedding)

        try:
            session.commit()
        except Exception:
            session.rollback()
            emp = _find_by_external_id(session, external_id)
            if emp is None:
                raise
            _apply(emp, first_name, file_id, embedding)
            session.commit()
            status = RACE_UPDATED

        session.refresh(emp)
        return _employee_to_dict(emp), status
    finally:
        session.close()


# ── Публичные операции ───────────────────────────────────────────────────────

def add_employee_from_file(first_name: str, file_id: int, external_id: int | None = None) -> dict:
    """Создаёт/обновляет одного сотрудника по фото из HR API (задача из api)."""
    embedding = _embedding_from_photo(file_id)
    result, _ = _save_employee(external_id, first_name, file_id, embedding)
    return result


def sync_employees() -> dict:
    """
    Полная синхронизация всех сотрудников HR.
    Возвращает {"added": N, "updated": N, "skipped": N, "errors": N}.
    """
    log.info("[employee_sync] Начало синхронизации сотрудников...")
    employees = _fetch_employees()

    counts = {"added": 0, "updated": 0, "skipped": 0, "errors": 0}
    if not employees:
        log.warning("[employee_sync] Список сотрудников пуст или недоступен")
        return counts

    for emp in employees:
        ext_id = emp.get("id")
        first_name = emp.get("firstname", "Unknown")
        file_id = emp.get("file", 0)
        who = f"id={ext_id} ({first_name})"

        if not file_id:
            log.info(f"[employee_sync] Сотрудник {who}: нет фото, пропускаем")
            counts["skipped"] += 1
            continue

        try:
            embedding = _embedding_from_photo(file_id)
        except PhotoUnavailableError:
            counts["errors"] += 1
            continue
        except EmbeddingMissingError:
            log.warning(f"[employee_sync] Сотрудник id={ext_id}: embedding=None")
            counts["skipped"] += 1
            continue
        except FaceNotFoundError:
            log.warning(
                f"[employee_sync] Сотрудник {who}: "
                f"лицо не найдено на фото file_id={file_id}"
            )
            counts["skipped"] += 1
            continue

        try:
            _, status = _save_employee(ext_id, first_name, file_id, embedding)
        except Exception as e:
            log.error(f"[employee_sync] Ошибка сохранения сотрудника id={ext_id}: {e}")
            counts["errors"] += 1
            continue

        if status == ADDED:
            counts["added"] += 1
            log.info(f"[employee_sync] Добавлен: {first_name} (external_id={ext_id})")
        elif status == UPDATED:
            counts["updated"] += 1
            log.info(f"[employee_sync] Обновлён: {first_name} (external_id={ext_id})")
        else:
            counts["updated"] += 1
            log.info(f"[employee_sync] Гонка INSERT разрешена UPDATE: {first_name} (external_id={ext_id})")

    log.info(f"[employee_sync] Завершено: {counts}")
    return counts
