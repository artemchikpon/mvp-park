"""Сотрудники: их лица не учитываются в статистике посетителей.

Эмбеддинги считает worker (там есть InsightFace); api только ставит задачи
через Redis (см. api/worker_jobs.py) и работает с таблицей employees.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.deps import get_db
from api.schemas import AddEmployeeRequest, EmployeeOut, SyncResult
from api.config import EMPLOYEE_JOB_TIMEOUT
from api.worker_jobs import (
    EnqueueError, WaitError, WorkerJobError, WorkerTimeout, run_worker_job,
)
from core.redis_keys import (
    EMPLOYEE_ADD_JOBS, EMPLOYEE_ADD_RESULT_PREFIX, EMPLOYEE_SYNC_JOBS,
    EMPLOYEE_SYNC_RESULT_PREFIX,
)
from db.models import Employee

router = APIRouter(prefix="/employees", tags=["employees"])

# Фразы из ошибок воркера, означающие "проблема в самом фото" -> 422.
_BAD_PHOTO_MARKERS = ("Лицо не найдено", "embedding")


@router.get("/", response_model=list[EmployeeOut], summary="Список сотрудников")
def list_employees(db: Session = Depends(get_db)):
    """Возвращает всех сотрудников в БД (без векторов)."""
    return db.query(Employee).order_by(Employee.created_at.desc()).all()


@router.post("/sync", response_model=SyncResult, summary="Синхронизировать всех сотрудников из HR API")
def sync_employees_route():
    """
    В api-сервисе нет face_engine/insightface (они тяжёлые и нужны только
    воркеру), поэтому синхронизация физически не может выполняться здесь.
    Вместо этого мы кладём задачу в Redis-очередь ("employee_sync_jobs"),
    один из worker-реплик её забирает, выполняет sync_employees() и кладёт
    результат обратно в Redis по ключу задания — а мы его ждём (blpop).
    """
    try:
        return run_worker_job(EMPLOYEE_SYNC_JOBS, EMPLOYEE_SYNC_RESULT_PREFIX)
    except EnqueueError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Не удалось поставить задачу синхронизации в очередь (Redis недоступен): {e}",
        )
    except WaitError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Ошибка ожидания результата синхронизации (Redis недоступен): {e}",
        )
    except WorkerTimeout:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Worker не ответил за {EMPLOYEE_JOB_TIMEOUT} сек. "
                "Проверьте что worker-сервис запущен и подключён к Redis."
            ),
        )
    except WorkerJobError as e:
        raise HTTPException(status_code=500, detail=f"Ошибка синхронизации в worker: {e.message}")


@router.post("/add", response_model=EmployeeOut, summary="Добавить сотрудника вручную")
def add_employee(body: AddEmployeeRequest):
    """Отправляет задачу в worker, где доступны face_engine/InsightFace."""
    try:
        return run_worker_job(EMPLOYEE_ADD_JOBS, EMPLOYEE_ADD_RESULT_PREFIX, body.model_dump())
    except (EnqueueError, WaitError) as e:
        raise HTTPException(status_code=503, detail=f"Redis недоступен: {e}") from e
    except WorkerTimeout:
        raise HTTPException(
            status_code=504,
            detail=f"Worker не ответил за {EMPLOYEE_JOB_TIMEOUT} сек.",
        )
    except WorkerJobError as e:
        status = 422 if any(m in e.message for m in _BAD_PHOTO_MARKERS) else 502
        raise HTTPException(status_code=status, detail=e.message)


@router.delete("/{employee_id}", summary="Удалить сотрудника и его векторы из БД")
def delete_employee(employee_id: int, db: Session = Depends(get_db)):
    emp = db.get(Employee, employee_id)
    if not emp:
        raise HTTPException(status_code=404, detail=f"Сотрудник id={employee_id} не найден")

    name = emp.first_name
    db.delete(emp)
    db.commit()
    return {"ok": True, "deleted_id": employee_id, "first_name": name}
