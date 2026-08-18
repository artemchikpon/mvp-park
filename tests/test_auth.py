"""
Тесты авторизации API (services/api/auth.py) — тот самый патч, закрывший
полностью открытый API (см. предыдущий разбор проблем проекта).
"""

import pytest
from fastapi import HTTPException

from api.auth import require_api_key


def test_require_api_key_accepts_valid_key():
    # Не должно бросать исключение
    require_api_key(x_api_key="test-key-1")
    require_api_key(x_api_key="test-key-2")


def test_require_api_key_rejects_missing_header():
    with pytest.raises(HTTPException) as exc_info:
        require_api_key(x_api_key=None)
    assert exc_info.value.status_code == 401


def test_require_api_key_rejects_wrong_key():
    with pytest.raises(HTTPException) as exc_info:
        require_api_key(x_api_key="totally-wrong-key")
    assert exc_info.value.status_code == 401


def test_require_api_key_rejects_empty_string():
    with pytest.raises(HTTPException):
        require_api_key(x_api_key="")


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
def test_all_business_routes_require_key(api_client, method, path):
    resp = api_client.request(method, path)
    assert resp.status_code == 401, f"{method} {path} должен требовать X-API-Key"
