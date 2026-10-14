from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.auth import authorize, require_admin
from api.bootstrap import ensure_initial_admin
from api.config import CORS_ORIGINS, ENABLE_DOCS
from api.routers import auth, cameras, employees, persons, settings, stats, users


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_initial_admin()
    yield


app = FastAPI(
    title="Park Tracker API",
    description="Управление камерами, статистика посещаемости (Вход/Выход, Live Occupancy), база лиц",
    version="2.0.0",
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# /health и /auth (вход) — публичные. Бизнес-роутеры: JWT или X-API-Key;
# viewer — только чтение, изменения — admin (см. auth.authorize).
app.include_router(auth.router)
for module in (cameras, stats, persons, settings, employees):
    app.include_router(module.router, dependencies=[Depends(authorize)])
app.include_router(users.router, dependencies=[Depends(require_admin)])


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}
