from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.auth import require_api_key
from api.config import CORS_ORIGINS, ENABLE_DOCS
from api.routers import cameras, employees, persons, settings, stats

app = FastAPI(
    title="Park Tracker API",
    description="Управление камерами, статистика посещаемости (Вход/Выход, Live Occupancy), база лиц",
    version="2.0.0",
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# /health остаётся публичным (docker healthcheck / балансировщик),
# все бизнес-роутеры требуют X-API-Key.
for module in (cameras, stats, persons, settings, employees):
    app.include_router(module.router, dependencies=[Depends(require_api_key)])


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}
