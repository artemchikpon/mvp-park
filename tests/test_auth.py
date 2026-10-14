"""
Тесты авторизации API (services/api/auth.py) — тот самый патч, закрывший
полностью открытый API (см. предыдущий разбор проблем проекта).
"""

import pytest

from api.auth import api_key_is_valid


def test_api_key_is_valid_accepts_configured_keys():
    assert api_key_is_valid("test-key-1")
    assert api_key_is_valid("test-key-2")


@pytest.mark.parametrize("key", [None, "", "totally-wrong-key"])
def test_api_key_is_valid_rejects_missing_empty_and_wrong(key):
    assert not api_key_is_valid(key)


# ── интеграционные проверки через реальные роуты FastAPI ───────────────────

def test_health_is_public_without_key(api_client):
    resp = api_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_protected_route_rejects_request_without_key(api_client):
    resp = api_client.get("/cameras/")
    assert resp.status_code == 401


def test_protected_route_rejects_wrong_key(api_client):
    resp = api_client.get("/cameras/", headers={"X-API-Key": "nope"})
    assert resp.status_code == 401


def test_protected_route_accepts_valid_key(api_client, api_key_header):
    resp = api_client.get("/cameras/", headers=api_key_header)
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
    ],
)
def test_all_business_routes_require_auth(api_client, method, path):
    resp = api_client.request(method, path)
    assert resp.status_code == 401, f"{method} {path} должен требовать JWT или X-API-Key"


# ── Swagger: кнопка Authorize ────────────────────────────────────────────────

def test_openapi_declares_api_key_and_oauth2_schemes(api_client):
    from api.main import app

    schema = app.openapi()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["APIKeyHeader"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": "Ключ из переменной окружения API_KEYS (права admin)",
    }
    assert schemes["OAuth2PasswordBearer"]["type"] == "oauth2"
    assert schemes["OAuth2PasswordBearer"]["flows"]["password"]["tokenUrl"] == "auth/login"
    # защищённые роуты ссылаются на обе схемы, публичные /health и /auth/login — нет
    security = schema["paths"]["/cameras/"]["get"]["security"]
    assert {"APIKeyHeader": []} in security and {"OAuth2PasswordBearer": []} in security
    assert "security" not in schema["paths"]["/health"]["get"]
    assert "security" not in schema["paths"]["/auth/login"]["post"]
    # заголовок не дублируется обычным параметром в каждом запросе
    params = schema["paths"]["/cameras/"]["get"].get("parameters", [])
    assert not any(p["name"].lower() == "x-api-key" for p in params)
