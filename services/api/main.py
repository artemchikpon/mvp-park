from fastapi import FastAPI
from api.routers import cameras, stats, persons, settings

app = FastAPI(
    title="Park Tracker API",
    description="Управление камерами, статистика посещаемости (Вход/Выход, Live Occupancy), база лиц",
    version="2.0.0",
)

app.include_router(cameras.router)
app.include_router(stats.router)
app.include_router(persons.router)
app.include_router(settings.router)


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}
