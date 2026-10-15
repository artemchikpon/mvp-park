"""JWT-авторизация администрации: вход, токены, роли admin/viewer, /users."""

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from api import security


def _login(client, username, password):
    return client.post("/auth/login", data={"username": username, "password": password})


# ── пароли и токены ──────────────────────────────────────────────────────────

def test_password_hash_roundtrip_and_salt():
    h1, h2 = security.hash_password("secret-123"), security.hash_password("secret-123")
    assert h1 != h2                                    # разная соль
    assert h1.startswith("scrypt$") and "secret-123" not in h1
    assert security.verify_password("secret-123", h1)
    assert not security.verify_password("secret-124", h1)
    assert not security.verify_password("secret-123", "garbage")


def test_access_and_refresh_tokens_are_not_interchangeable():
    access, refresh = security.create_access_token(7), security.create_refresh_token(7)
    assert security.decode_token(access, security.ACCESS) == 7
    assert security.decode_token(refresh, security.REFRESH) == 7
    with pytest.raises(security.InvalidToken):
        security.decode_token(refresh, security.ACCESS)
    with pytest.raises(security.InvalidToken):
        security.decode_token(access, security.REFRESH)


def test_expired_and_forged_tokens_are_rejected():
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    expired = jwt.encode({"sub": "1", "type": "access", "exp": past}, security.JWT_SECRET, algorithm="HS256")
    forged = jwt.encode({"sub": "1", "type": "access", "exp": past + timedelta(hours=1)},
                        "another-secret-" + "y" * 32, algorithm="HS256")
    for token in (expired, forged, "not-a-jwt"):
        with pytest.raises(security.InvalidToken):
            security.decode_token(token, security.ACCESS)


# ── /auth ────────────────────────────────────────────────────────────────────

def test_login_returns_token_pair_and_me(api_client, make_user):
    make_user(username="boss", password="boss-pass-123", role="admin")
    resp = _login(api_client, "boss", "boss-pass-123")
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == security.ACCESS_TOKEN_MINUTES * 60

    me = api_client.get("/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json() == {"user_id": 1, "username": "boss", "role": "admin"}


@pytest.mark.parametrize("username,password", [("boss", "wrong-pass-1"), ("nobody", "boss-pass-123")])
def test_login_rejects_wrong_credentials(api_client, make_user, username, password):
    make_user(username="boss", password="boss-pass-123")
    resp = _login(api_client, username, password)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Неверный логин или пароль"


def test_login_rejects_inactive_user(api_client, make_user):
    make_user(username="old", password="old-pass-123", is_active=False)
    assert _login(api_client, "old", "old-pass-123").status_code == 401


def test_refresh_gives_new_pair_and_rejects_access_token(api_client, make_user):
    make_user(username="boss", password="boss-pass-123")
    tokens = _login(api_client, "boss", "boss-pass-123").json()

    resp = api_client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 200
    new_access = resp.json()["access_token"]
    assert api_client.get("/cameras/", headers={"Authorization": f"Bearer {new_access}"}).status_code == 200

    assert api_client.post("/auth/refresh", json={"refresh_token": tokens["access_token"]}).status_code == 401


def test_refresh_token_cannot_be_used_for_requests(api_client, make_user):
    user = make_user()
    refresh = security.create_refresh_token(user.id)
    assert api_client.get("/cameras/", headers={"Authorization": f"Bearer {refresh}"}).status_code == 401


def test_deactivated_user_loses_access_immediately(api_client, make_user, db_session):
    user = make_user(username="temp", role="admin")
    headers = {"Authorization": f"Bearer {security.create_access_token(user.id)}"}
    assert api_client.get("/cameras/", headers=headers).status_code == 200

    user.is_active = False
    db_session.commit()
    assert api_client.get("/cameras/", headers=headers).status_code == 401


# ── роли ─────────────────────────────────────────────────────────────────────

CAMERA = {"name": "cam", "url": "rtsp://example/stream", "direction": "in"}


def test_viewer_can_read_but_not_change(api_client, bearer):
    viewer = bearer("viewer")
    for path in ("/cameras/", "/stats/live", "/settings/", "/persons/count", "/employees/"):
        assert api_client.get(path, headers=viewer).status_code == 200, path

    assert api_client.post("/cameras/", json=CAMERA, headers=viewer).status_code == 403
    assert api_client.put("/settings/", json={"capacity": 6000}, headers=viewer).status_code == 403
    assert api_client.post("/persons/reset", headers=viewer).status_code == 403


def test_admin_can_change(api_client, bearer):
    admin = bearer("admin")
    assert api_client.post("/cameras/", json=CAMERA, headers=admin).status_code == 201
    assert api_client.put("/settings/", json={"capacity": 6000}, headers=admin).status_code == 200


# ── /users ───────────────────────────────────────────────────────────────────

def test_users_are_admin_only(api_client, bearer):
    viewer = bearer("viewer")
    assert api_client.get("/users/", headers=viewer).status_code == 403
    assert api_client.post("/users/", json={"username": "x" * 5, "password": "p" * 8},
                           headers=viewer).status_code == 403


def test_admin_manages_users_and_new_user_can_login(api_client, bearer):
    admin = bearer("admin")
    resp = api_client.post("/users/", json={"username": "guard", "password": "guard-pass-1", "role": "viewer"},
                           headers=admin)
    assert resp.status_code == 201
    created = resp.json()
    assert created["role"] == "viewer" and created["is_active"] is True
    assert "password" not in created and "password_hash" not in created

    assert api_client.post("/users/", json={"username": "guard", "password": "other-pass-1"},
                           headers=admin).status_code == 409
    assert _login(api_client, "guard", "guard-pass-1").status_code == 200

    uid = created["id"]
    upd = api_client.patch(f"/users/{uid}", json={"password": "new-pass-123", "role": "admin"}, headers=admin)
    assert upd.status_code == 200 and upd.json()["role"] == "admin"
    assert _login(api_client, "guard", "guard-pass-1").status_code == 401
    assert _login(api_client, "guard", "new-pass-123").status_code == 200

    assert api_client.delete(f"/users/{uid}", headers=admin).status_code == 204
    assert _login(api_client, "guard", "new-pass-123").status_code == 401


@pytest.mark.parametrize("body", [{"username": "ab", "password": "long-enough"},
                                  {"username": "fine-name", "password": "short"},
                                  {"username": "fine-name", "password": "long-enough", "role": "root"}])
def test_user_create_validation(api_client, bearer, body):
    assert api_client.post("/users/", json=body, headers=bearer("admin")).status_code == 422


def test_admin_cannot_lock_themselves_out(api_client, make_user):
    me = make_user(username="boss", role="admin")
    headers = {"Authorization": f"Bearer {security.create_access_token(me.id)}"}
    assert api_client.delete(f"/users/{me.id}", headers=headers).status_code == 400
    assert api_client.patch(f"/users/{me.id}", json={"is_active": False}, headers=headers).status_code == 400
    assert api_client.patch(f"/users/{me.id}", json={"role": "viewer"}, headers=headers).status_code == 400
    # сменить себе пароль можно
    assert api_client.patch(f"/users/{me.id}", json={"password": "changed-123"}, headers=headers).status_code == 200


# ── первый admin из окружения ────────────────────────────────────────────────

def test_initial_admin_created_only_when_no_users(db_session, monkeypatch):
    from api.bootstrap import ensure_initial_admin
    from db.models import User

    monkeypatch.setenv("ADMIN_USERNAME", "root")
    monkeypatch.setenv("ADMIN_PASSWORD", "root-pass-123")
    ensure_initial_admin()
    user = db_session.query(User).one()
    assert user.username == "root" and user.role == "admin"
    assert security.verify_password("root-pass-123", user.password_hash)

    # таблица уже не пуста — новый пароль из окружения ничего не меняет
    monkeypatch.setenv("ADMIN_PASSWORD", "another-pass-1")
    ensure_initial_admin()
    db_session.expire_all()
    assert db_session.query(User).count() == 1
    assert security.verify_password("root-pass-123", db_session.query(User).one().password_hash)


def test_no_initial_admin_without_env(db_session, monkeypatch):
    from api.bootstrap import ensure_initial_admin
    from db.models import User

    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    ensure_initial_admin()
    assert db_session.query(User).count() == 0
