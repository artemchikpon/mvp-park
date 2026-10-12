# Park Tracker (MVP)

Подсчёт посетителей парка по камерам на входах и выходах: лица с видеопотоков
распознаются, сотрудники отфильтровываются, каждое пересечение линии
вход/выход сохраняется в Postgres, а API отдаёт заполненность парка,
графики и демографию для дашборда.

```
камеры (RTSP) ──> producer ──> Redis Stream "frames" ──> worker ×N ──> Postgres <── api (FastAPI)
                                                          │   ▲                      │
                                     HR API (сотрудники) ─┘   └── очереди задач ─────┘
                                                                  employee_* (Redis)
```

## Сервисы

| Сервис | Код | Что делает |
|---|---|---|
| `migrator` | `shared/migrations` | `alembic upgrade head` при старте стека |
| `producer` | `services/producer/camera.py` | читает активные камеры из БД, шлёт JPEG-кадры в стрим |
| `worker` | `services/worker/` | лица → сотрудник? → галерея `persons` → событие `crossing_events` |
| `api` | `services/api/` | REST: камеры, статистика, настройки, persons, сотрудники (заголовок `X-API-Key`) |

### Модули worker

| Модуль | Ответственность |
|---|---|
| `worker.py` | точка входа: ожидание БД/Redis, запуск фоновых потоков и чтения стрима |
| `stream_consumer.py` | consumer group, XACK, XAUTOCLAIM зависших сообщений |
| `crossings.py` | обработка кадра, cooldown повторного счёта (Redis), запись события |
| `face_storage_pg.py` | сопоставление embedding'ов (сотрудники / persons), кэш в памяти |
| `embeddings.py` | сериализация векторов в bytes и обратно |
| `face_engine.py` | InsightFace (`buffalo_sc`), модель загружается при импорте |
| `camera_cache.py` | кэш direction/gate камер |
| `persons_reset.py` | ежедневная очистка галереи `persons` (Redis-лок между репликами) |
| `employee_jobs.py` | задачи `sync`/`add` от api через Redis |
| `employee_sync.py` | загрузка сотрудников и их фото из HR API |
| `worker_config.py` | все настройки воркера из окружения |

### Общий код (`shared/`)

- `db/` — SQLAlchemy-модели, сессии, `stats_service.py` (вся аналитика).
- `core/` — чтение env, `utcnow()`, фабрика Redis-клиента, имена Redis-ключей,
  общие для нескольких сервисов (`redis_keys.py`).

Оба пакета копируются в каждый образ (`COPY shared/db ./db`, `COPY shared/core ./core`).

## Запуск

```bash
echo "API_KEYS=$(openssl rand -hex 24)" > .env
docker compose up -d --build
# Swagger: http://localhost:8000/docs (ENABLE_DOCS=true в compose)
```

Тестовые данные для дашборда (Postgres из compose должен быть доступен на localhost):

```bash
DATABASE_URL=postgresql://face:face@localhost:5432/face_db python scripts/seed_dashboard.py
```

## Переменные окружения

| Переменная | Где | По умолчанию | Назначение |
|---|---|---|---|
| `DATABASE_URL` | все | `postgresql://face:face@postgres:5432/face_db` | подключение к Postgres |
| `REDIS_HOST` / `REDIS_PORT` | api, worker, producer | `redis` / `6379` | Redis |
| `API_KEYS` | api | — (обязательно) | ключи для `X-API-Key`, через запятую |
| `CORS_ORIGINS` | api | `*` | разрешённые origin'ы дашборда |
| `ENABLE_DOCS` | api | `false` | `/docs`, `/redoc`, `/openapi.json` |
| `EMPLOYEE_SYNC_TIMEOUT` | api | `120` | сколько ждать ответа воркера, сек |
| `HR_API_BASE` / `HR_SSL_VERIFY` | worker | — / `true` | HR-система с сотрудниками |
| `HR_EMPLOYEES_PAGE_LIMIT` | worker | `100` | размер страницы HR API |
| `CROSSING_COOLDOWN_SECONDS` | worker | `60` | анти-дубль одного человека на одной камере |
| `PERSONS_RESET_HOUR` / `_MINUTE` | worker | `12` / `0` | время ежедневной очистки `persons` (таймзона парка) |

## Разработка

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

Новая миграция (модели в `shared/db/models.py` синхронизированы со схемой,
поэтому autogenerate на актуальной БД даёт пустую миграцию):

```bash
cd shared && DATABASE_URL=postgresql://face:face@localhost:5432/face_db \
  PYTHONPATH=. alembic revision --autogenerate -m "описание"
```
