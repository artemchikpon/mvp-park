import os

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware

from api import routers
from api.routers import cameras, stats, persons, settings, employees
from api.auth import require_api_key


ENABLE_DOCS = os.environ.get("ENABLE_DOCS", "false").lower() == "true"

app = FastAPI(
    title="Park Tracker API",
    description="Управление камерами, статистика посещаемости (Вход/Выход, Live Occupancy), база лиц",
    version="2.0.0",
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)

# CORS для отдельного dashboard/frontend. В production задайте CORS_ORIGINS
# через .env, например: https://dashboard.example.com,https://admin.example.com
_cors_origins = [x.strip() for x in os.environ.get("CORS_ORIGINS", "*").split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# /health остаётся публичным (нужен для docker healthcheck / балансировщика),
# все остальные роутеры требуют X-API-Key.
_auth = [Depends(require_api_key)]

app.include_router(cameras.router, dependencies=_auth)
app.include_router(stats.router, dependencies=_auth)
app.include_router(persons.router, dependencies=_auth)
app.include_router(settings.router, dependencies=_auth)
app.include_router(employees.router, dependencies=_auth)


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}
