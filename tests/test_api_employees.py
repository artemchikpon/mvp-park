"""
Тесты для services/api/routers/employees.py и RPC api -> worker
(api/worker_jobs.py <-> worker/employee_jobs.py) поверх fakeredis.
"""

import json
import uuid

import fakeredis
import pytest

import employee_jobs
import redis_conn
from api import worker_jobs
from core.redis_keys import (
    EMPLOYEE_ADD_JOBS, EMPLOYEE_ADD_RESULT_PREFIX, EMPLOYEE_SYNC_JOBS,
    EMPLOYEE_SYNC_RESULT_PREFIX, result_key,
)
from db.models import Employee

FIXED_JOB_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture()
def fake_redis(monkeypatch):
    server = fakeredis.FakeServer()
    api_client = fakeredis.FakeRedis(server=server, decode_responses=True)
    monkeypatch.setattr(worker_jobs, "make_redis", lambda **kw: api_client)
    monkeypatch.setattr(worker_jobs.uuid, "uuid4", lambda: FIXED_JOB_ID)
    monkeypatch.setattr(worker_jobs, "EMPLOYEE_JOB_TIMEOUT", 1)
    return api_client


def _prepare_worker_reply(client, prefix: str, reply: dict) -> None:
    """Кладём ответ воркера заранее: BLPOP в api заберёт его сразу."""
    client.rpush(result_key(prefix, str(FIXED_JOB_ID)), json.dumps(reply))


# ── list / delete ────────────────────────────────────────────────────────────

def test_list_employees_hides_embeddings(api_client, api_key_header, db_session):
    db_session.add(Employee(first_name="Иван", external_id=7, file_id=3, embedding=b"\x00" * 8))
    db_session.commit()

    resp = api_client.get("/employees/", headers=api_key_header)
    assert resp.status_code == 200
    [emp] = resp.json()
    assert emp["first_name"] == "Иван"
    assert "embedding" not in emp


def test_delete_employee(api_client, api_key_header, db_session):
    emp = Employee(first_name="Иван", embedding=b"\x00" * 8)
    db_session.add(emp)
    db_session.commit()

    resp = api_client.delete(f"/employees/{emp.id}", headers=api_key_header)
    assert resp.json() == {"ok": True, "deleted_id": emp.id, "first_name": "Иван"}
    assert api_client.delete(f"/employees/{emp.id}", headers=api_key_header).status_code == 404


# ── sync / add через очередь ─────────────────────────────────────────────────

def test_sync_enqueues_job_and_returns_worker_result(api_client, api_key_header, fake_redis):
    reply = {"added": 2, "updated": 1, "skipped": 0, "errors": 0}
    _prepare_worker_reply(fake_redis, EMPLOYEE_SYNC_RESULT_PREFIX, reply)

    resp = api_client.post("/employees/sync", headers=api_key_header)

    assert resp.status_code == 200
    assert resp.json() == reply
    assert json.loads(fake_redis.lpop(EMPLOYEE_SYNC_JOBS)) == {"job_id": str(FIXED_JOB_ID)}


def test_sync_worker_error_is_500(api_client, api_key_header, fake_redis):
    _prepare_worker_reply(fake_redis, EMPLOYEE_SYNC_RESULT_PREFIX, {"error": "HR down"})
    resp = api_client.post("/employees/sync", headers=api_key_header)
    assert resp.status_code == 500
    assert "HR down" in resp.json()["detail"]


def test_sync_times_out_with_504_when_no_worker(api_client, api_key_header, fake_redis):
    resp = api_client.post("/employees/sync", headers=api_key_header)
    assert resp.status_code == 504


def test_add_sends_payload_to_worker(api_client, api_key_header, fake_redis):
    reply = {"id": 1, "external_id": 5, "first_name": "Анна", "file_id": 9, "created_at": None}
    _prepare_worker_reply(fake_redis, EMPLOYEE_ADD_RESULT_PREFIX, reply)

    resp = api_client.post("/employees/add", headers=api_key_header,
                           json={"first_name": "Анна", "file_id": 9, "external_id": 5})

    assert resp.status_code == 200
    assert resp.json()["first_name"] == "Анна"
    job = json.loads(fake_redis.lpop(EMPLOYEE_ADD_JOBS))
    assert job == {"job_id": str(FIXED_JOB_ID), "first_name": "Анна", "file_id": 9, "external_id": 5}


@pytest.mark.parametrize("message,status", [
    ("Лицо не найдено на фотографии", 422),
    ("Не удалось извлечь embedding лица", 422),
    ("Не удалось скачать или декодировать фото file_id=9", 502),
])
def test_add_maps_worker_errors_to_status(api_client, api_key_header, fake_redis, message, status):
    _prepare_worker_reply(fake_redis, EMPLOYEE_ADD_RESULT_PREFIX, {"error": message})
    resp = api_client.post("/employees/add", headers=api_key_header,
                           json={"first_name": "Анна", "file_id": 9})
    assert resp.status_code == status
    assert resp.json()["detail"] == message


# ── сторона воркера: employee_jobs.handle_job ────────────────────────────────

@pytest.fixture()
def worker_redis(monkeypatch):
    fr = fakeredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr(redis_conn, "r", fr)
    return fr


def test_worker_handles_add_job_and_replies(worker_redis, monkeypatch):
    calls = []

    def fake_add(first_name, file_id, external_id):
        calls.append((first_name, file_id, external_id))
        return {"id": 1, "first_name": first_name}

    monkeypatch.setattr(employee_jobs.employee_sync, "add_employee_from_file", fake_add)
    payload = json.dumps({"job_id": "j1", "first_name": "Анна", "file_id": "9", "external_id": 5})

    employee_jobs.handle_job(EMPLOYEE_ADD_JOBS, payload.encode())

    assert calls == [("Анна", 9, 5)]
    key = result_key(EMPLOYEE_ADD_RESULT_PREFIX, "j1")
    assert 0 < worker_redis.ttl(key)  # ответ не копится вечно, если api уже не ждёт
    assert json.loads(worker_redis.lpop(key)) == {"id": 1, "first_name": "Анна"}


def test_worker_replies_with_error_and_accepts_legacy_plain_job_id(worker_redis, monkeypatch):
    def boom():
        raise RuntimeError("HR down")

    monkeypatch.setattr(employee_jobs.employee_sync, "sync_employees", boom)

    employee_jobs.handle_job(EMPLOYEE_SYNC_JOBS, b"legacy-id")

    reply = json.loads(worker_redis.lpop(result_key(EMPLOYEE_SYNC_RESULT_PREFIX, "legacy-id")))
    assert reply == {"error": "HR down"}
