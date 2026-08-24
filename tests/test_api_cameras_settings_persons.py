"""
CRUD-тесты для services/api/routers/cameras.py, settings.py, persons.py.
employees.py тестируется отдельно (там больше внешних зависимостей — HR API,
face_engine), см. test_api_employees.py.
"""

import pytest

from db.models import Person


# ── cameras ──────────────────────────────────────────────────────────────────

def test_create_camera_returns_201_and_persisted_fields(api_client, api_key_header):
    payload = {
        "name": "Вход северный",
        "url": "rtsp://user:pass@10.0.0.5/stream1",
        "direction": "in",
        "gate": "Центральный",
    }
    resp = api_client.post("/cameras/", json=payload, headers=api_key_header)
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] is not None
    assert body["name"] == "Вход северный"
    assert body["direction"] == "in"
    assert body["active"] is True  # дефолт


def test_create_camera_rejects_empty_url(api_client, api_key_header):
    payload = {"name": "Cam", "url": "   ", "direction": "in"}
    resp = api_client.post("/cameras/", json=payload, headers=api_key_header)
    assert resp.status_code == 422


def test_create_camera_rejects_invalid_direction(api_client, api_key_header):
    payload = {"name": "Cam", "url": "rtsp://x", "direction": "sideways"}
    resp = api_client.post("/cameras/", json=payload, headers=api_key_header)
    assert resp.status_code == 422


def test_list_cameras_filters_by_direction_and_gate(api_client, api_key_header):
    api_client.post("/cameras/", headers=api_key_header, json={
        "name": "A", "url": "rtsp://a", "direction": "in", "gate": "G1"})
    api_client.post("/cameras/", headers=api_key_header, json={
        "name": "B", "url": "rtsp://b", "direction": "out", "gate": "G1"})
    api_client.post("/cameras/", headers=api_key_header, json={
        "name": "C", "url": "rtsp://c", "direction": "in", "gate": "G2"})

    resp = api_client.get("/cameras/", headers=api_key_header, params={"direction": "in"})
    names = {c["name"] for c in resp.json()}
    assert names == {"A", "C"}

    resp = api_client.get("/cameras/", headers=api_key_header, params={"gate": "G1"})
    names = {c["name"] for c in resp.json()}
    assert names == {"A", "B"}


def test_get_camera_404_when_missing(api_client, api_key_header):
    resp = api_client.get("/cameras/9999", headers=api_key_header)
    assert resp.status_code == 404


def test_update_camera_patches_only_given_fields(api_client, api_key_header):
    created = api_client.post("/cameras/", headers=api_key_header, json={
        "name": "Old name", "url": "rtsp://x", "direction": "in", "gate": "G1"}).json()

    resp = api_client.patch(
        f"/cameras/{created['id']}", headers=api_key_header, json={"name": "New name"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "New name"
    assert body["gate"] == "G1"  # не тронуто


def test_toggle_camera_flips_active_flag(api_client, api_key_header):
    created = api_client.post("/cameras/", headers=api_key_header, json={
        "name": "Cam", "url": "rtsp://x", "direction": "in"}).json()
    assert created["active"] is True

    resp = api_client.post(f"/cameras/{created['id']}/toggle", headers=api_key_header)
    assert resp.json()["active"] is False

    resp = api_client.post(f"/cameras/{created['id']}/toggle", headers=api_key_header)
    assert resp.json()["active"] is True


def test_delete_camera_removes_it(api_client, api_key_header):
    created = api_client.post("/cameras/", headers=api_key_header, json={
        "name": "Cam", "url": "rtsp://x", "direction": "in"}).json()

    resp = api_client.delete(f"/cameras/{created['id']}", headers=api_key_header)
    assert resp.status_code == 204

    resp = api_client.get(f"/cameras/{created['id']}", headers=api_key_header)
    assert resp.status_code == 404


def test_list_gates_aggregates_in_out_counts_per_gate(api_client, api_key_header):
    api_client.post("/cameras/", headers=api_key_header, json={
        "name": "A", "url": "rtsp://a", "direction": "in", "gate": "Центральный"})
    api_client.post("/cameras/", headers=api_key_header, json={
        "name": "B", "url": "rtsp://b", "direction": "in", "gate": "Центральный"})
    api_client.post("/cameras/", headers=api_key_header, json={
        "name": "C", "url": "rtsp://c", "direction": "out", "gate": "Центральный"})

    resp = api_client.get("/cameras/gates", headers=api_key_header)
    gates = {g["gate"]: g for g in resp.json()}
    assert gates["Центральный"]["in_cameras"] == 2
    assert gates["Центральный"]["out_cameras"] == 1


# ── settings ─────────────────────────────────────────────────────────────────

def test_get_settings_returns_defaults(api_client, api_key_header):
    resp = api_client.get("/settings/", headers=api_key_header)
    assert resp.status_code == 200
    body = resp.json()
    assert body["capacity"] == 5000
    assert body["timezone"] == "UTC"


def test_update_settings_persists_changes(api_client, api_key_header):
    resp = api_client.put(
        "/settings/", headers=api_key_header,
        json={"capacity": 2000, "warning_threshold": 1800, "timezone": "Asia/Tashkent"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["capacity"] == 2000
    assert body["timezone"] == "Asia/Tashkent"

    # изменения видны при повторном GET
    resp = api_client.get("/settings/", headers=api_key_header)
    assert resp.json()["capacity"] == 2000


def test_update_settings_rejects_non_positive_capacity(api_client, api_key_header):
    resp = api_client.put("/settings/", headers=api_key_header, json={"capacity": 0})
    assert resp.status_code == 422

    resp = api_client.put("/settings/", headers=api_key_header, json={"capacity": -10})
    assert resp.status_code == 422


# ── persons ──────────────────────────────────────────────────────────────────

def test_list_persons_empty_by_default(api_client, api_key_header):
    resp = api_client.get("/persons/", headers=api_key_header)
    assert resp.status_code == 200
    assert resp.json() == []


def test_count_persons_matches_db(api_client, api_key_header, db_session):
    for i in range(3):
        db_session.add(Person(embedding=b"\x00" * 2048, age=20 + i, gender=i % 2))
    db_session.commit()

    resp = api_client.get("/persons/count", headers=api_key_header)
    assert resp.json() == {"count": 3}


def test_list_persons_respects_limit_and_offset(api_client, api_key_header, db_session):
    for i in range(5):
        db_session.add(Person(embedding=b"\x00" * 2048, age=i))
    db_session.commit()

    resp = api_client.get("/persons/", headers=api_key_header, params={"limit": 2, "offset": 1})
    assert len(resp.json()) == 2


def test_list_persons_limit_out_of_range_is_rejected(api_client, api_key_header):
    resp = api_client.get("/persons/", headers=api_key_header, params={"limit": 0})
    assert resp.status_code == 422
    resp = api_client.get("/persons/", headers=api_key_header, params={"limit": 501})
    assert resp.status_code == 422


def test_reset_persons_clears_table_and_returns_deleted_count(api_client, api_key_header, db_session):
    for i in range(3):
        db_session.add(Person(embedding=b"\x00" * 2048, age=20 + i, gender=i % 2))
    db_session.commit()

    resp = api_client.post("/persons/reset", headers=api_key_header)
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 3}

    resp = api_client.get("/persons/count", headers=api_key_header)
    assert resp.json() == {"count": 0}


def test_reset_persons_nulls_person_id_on_crossing_events(api_client, api_key_header, db_session):
    from db.models import Camera, CrossingEvent

    cam = Camera(name="A", url="rtsp://a", direction="in")
    db_session.add(cam)
    db_session.commit()

    person = Person(embedding=b"\x00" * 2048, age=20, gender=1)
    db_session.add(person)
    db_session.commit()
    db_session.refresh(person)

    event = CrossingEvent(camera_id=cam.id, direction="in", person_id=person.id)
    db_session.add(event)
    db_session.commit()
    event_id = event.id

    resp = api_client.post("/persons/reset", headers=api_key_header)
    assert resp.status_code == 200

    db_session.expire_all()
    kept = db_session.get(CrossingEvent, event_id)
    assert kept is not None
    assert kept.person_id is None
