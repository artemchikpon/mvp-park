"""
Тесты авторизации API (services/api/auth.py): все бизнес-роуты закрыты,
войти можно только по JWT. Подробные сценарии JWT — в test_jwt_auth.py.
"""

import pytest


def test_health_is_public(api_client):
    resp = api_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_protected_route_rejects_request_without_token(api_client):
    resp = api_client.get("/cameras/")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_protected_route_rejects_garbage_token(api_client):
    resp = api_client.get("/cameras/", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_api_key_header_no_longer_grants_access(api_client):
    resp = api_client.get("/cameras/", headers={"X-API-Key": "test-key-1"})
    assert resp.status_code == 401


def test_protected_route_accepts_valid_token(api_client, auth_header):
    resp = api_client.get("/cameras/", headers=auth_header)
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/cameras/"),
        ("GET", "/persons/"),
        ("GET", "/employees/"),
        ("GET", "/settings/"),
        ("GET", "/stats/live"),
        ("GET", "/users/"),
        ("GET", "/auth/me"),
    ],
)
def test_all_business_routes_require_auth(api_client, method, path):
    resp = api_client.request(method, path)
    assert resp.status_code == 401, f"{method} {path} должен требовать JWT"


# ── Swagger: кнопка Authorize ────────────────────────────────────────────────

def test_openapi_declares_only_oauth2_scheme(api_client):
    from api.main import app

    schema = app.openapi()
    schemes = schema["components"]["securitySchemes"]
    assert list(schemes) == ["OAuth2PasswordBearer"]
    assert schemes["OAuth2PasswordBearer"]["type"] == "oauth2"
    assert schemes["OAuth2PasswordBearer"]["flows"]["password"]["tokenUrl"] == "auth/login"
    # защищённые роуты ссылаются на схему, публичные /health и /auth/login — нет
    assert schema["paths"]["/cameras/"]["get"]["security"] == [{"OAuth2PasswordBearer": []}]
    assert "security" not in schema["paths"]["/health"]["get"]
    assert "security" not in schema["paths"]["/auth/login"]["post"]
