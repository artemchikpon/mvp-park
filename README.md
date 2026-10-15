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
| `api` | `services/api/` | REST: камеры, статистика, настройки, persons, сотрудники, пользователи (JWT) |

### Модули worker

| Модуль | Ответственность |
|---|---|
| `worker.py` | точка входа: ожидание БД/Redis, запуск фоновых потоков и чтения стрима |
| `stream_consumer.py` | consumer group, XACK, XAUTOCLAIM зависших сообщений |
| `crossings.py` | обработка кадра, cooldown повторного счёта (Redis), запись события |
| `face_storage_pg.py` | поиск похожего лица (сотрудники / persons) запросом pgvector в Postgres |
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
echo "JWT_SECRET=$(openssl rand -hex 32)" > .env
echo "ADMIN_USERNAME=admin" >> .env
echo "ADMIN_PASSWORD=смените-этот-пароль" >> .env
docker compose up -d --build
# Swagger: http://localhost:8000/docs (ENABLE_DOCS=true в compose)
```

## Авторизация

- **JWT — для администрации.** `POST /auth/login` (логин/пароль, форма OAuth2)
  возвращает `access_token` (30 мин) и `refresh_token` (7 дней). Запросы — с
  заголовком `Authorization: Bearer <access_token>`; новая пара без пароля —
  `POST /auth/refresh`. Кто я — `GET /auth/me`.
- **Роли:** `admin` — всё; `viewer` — только чтение (GET), изменения → `403`.
  Отключение пользователя и смена роли действуют сразу.
- **Пользователи** — `/users` (только admin). Первый admin создаётся при старте
  из `ADMIN_USERNAME` / `ADMIN_PASSWORD`, пока таблица `users` пуста; дальше эти
  переменные ни на что не влияют.

В Swagger кнопка **Authorize**: логин и пароль (поля client_id/client_secret не нужны).

**Таймзона парка** — `Asia/Tashkent` по умолчанию (`park_settings.timezone`).
В ней считаются «сегодня», почасовые и дневные графики и время сброса
`persons`; сами события в БД хранятся в UTC. Сменить: `PUT /settings/`
с `{"timezone": "..."}`.

Тестовые данные для дашборда (Postgres из compose должен быть доступен на localhost):

```bash
DATABASE_URL=postgresql://face:face@localhost:5432/face_db python scripts/seed_dashboard.py
```

## Переменные окружения

| Переменная | Где | По умолчанию | Назначение |
|---|---|---|---|
| `DATABASE_URL` | все | `postgresql://face:face@postgres:5432/face_db` | подключение к Postgres |
| `REDIS_HOST` / `REDIS_PORT` | api, worker, producer | `redis` / `6379` | Redis |
| `JWT_SECRET` | api | — (обязательно, ≥ 32 символов) | подпись JWT-токенов |
| `ACCESS_TOKEN_MINUTES` / `REFRESH_TOKEN_DAYS` | api | `30` / `7` | срок жизни токенов |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | api | — | первый admin, пока таблица `users` пуста |
| `CORS_ORIGINS` | api | `*` | разрешённые origin'ы дашборда |
| `ENABLE_DOCS` | api | `false` | `/docs`, `/redoc`, `/openapi.json` |
| `EMPLOYEE_SYNC_TIMEOUT` | api | `120` | сколько ждать ответа воркера, сек |
| `HR_API_BASE` / `HR_SSL_VERIFY` | worker | — / `true` | HR-система с сотрудниками |
| `HR_EMPLOYEES_PAGE_LIMIT` | worker | `100` | размер страницы HR API |
| `PERF_LOG_INTERVAL` | worker, producer | `30` | раз в N секунд писать в лог сводку `[perf ...]`: время шагов, CPU процесса, кадры/с; `0` — выключить |
| `FACE_DEVICE` | worker | `cpu` | где считать InsightFace: `cpu` или `gpu` (CUDA; без GPU сам откатывается на CPU) |
| `FACE_CPU_THREADS` | worker | `0` | потоков CPU на одну модель InsightFace; `0` — решает onnxruntime. Потоки засыпают между кадрами (spinning выключен), поэтому простаивающий воркер не грузит CPU |
| `WORKER_ROLE` | worker | `primary` | `overflow` — подстраховка: берёт кадры, только если очередь копится |
| `OVERFLOW_MIN_BACKLOG` | worker | `10` | с какой очереди (невыданных кадров) подключается `overflow` |
| `PRODUCER_MAX_FPS` | producer | `5` | максимум кадров в секунду с одной камеры в Redis; `0` — без ограничения |
| `CROSSING_COOLDOWN_SECONDS` | worker | `60` | анти-дубль одного человека на одной камере |
| `PERSONS_RESET_HOUR` / `_MINUTE` | worker | `0` / `0` | время ежедневной очистки `persons` (таймзона парка), по умолчанию 00:00 |

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

## Векторы лиц и pgvector

Векторы лиц (`persons.embedding`, `employees.embedding`) хранятся в колонках
`vector(512)` расширения [pgvector](https://github.com/pgvector/pgvector);
Postgres в compose — образ `pgvector/pgvector:0.8.0-pg15`. Самое похожее лицо
ищется прямо в базе:

```sql
SELECT id, 1 - (embedding <=> :face) AS similarity   -- <=> косинусное расстояние
FROM persons ORDER BY embedding <=> :face, id LIMIT 1;
```

Совпадение — при сходстве `>= 0.65` (`SIMILARITY` в `face_storage_pg.py`).
Копии галереи в памяти воркеров нет: все реплики ищут по одной таблице, а
«поиск + создание нового человека» идут под общим advisory lock, поэтому две
реплики не заведут одного человека дважды.

Поиск точный (перебор всех строк). Для дневной галереи на тысячи человек это
миллисекунды; если она вырастет до сотен тысяч, можно добавить
приблизительный индекс (результат может изредка отличаться от точного):

```sql
CREATE INDEX ON persons USING hnsw (embedding vector_cosine_ops);
```

## Замеры производительности

Worker и producer раз в `PERF_LOG_INTERVAL` секунд пишут строку `[perf ...]`:

```bash
docker compose logs -f worker producer | grep perf
```

- `CPU N cores` — сколько ядер в среднем занимал процесс за интервал;
- worker: `frames`, `faces`, `crossings`, `new_persons` (в секунду); время шагов
  `model` (InsightFace), `match` (поиск похожего лица в Postgres),
  `db_person`, `db_event`, `decode`, `redis_cooldown` и их доля;
  `queue_wait` — сколько кадр ждал в очереди (растёт, если воркеры не
  успевают);
- producer: по каждой камере `grabbed` (прочитано из потока) и `sent`
  (отправлено в Redis) в секунду, время `retrieve`, `jpeg`, `xadd`.

## Распознавание на видеокарте (GPU / CPU)

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

- `worker-gpu` — InsightFace на NVIDIA через CUDA (`services/worker/Dockerfile.gpu`,
  `onnxruntime-gpu` 1.26 + библиотеки CUDA 12.6 из pip), основной воркер;
- `worker` (CPU) — `WORKER_ROLE=overflow`: пока GPU справляется, не берёт
  кадры и не тратит CPU; если очередь выросла до `OVERFLOW_MIN_BACKLOG`
  (GPU не успевает или упал) — подключается сам и подбирает зависшие кадры.

Требования к хосту: видеокарта NVIDIA, драйвер с поддержкой CUDA >= 12.6,
NVIDIA Container Toolkit (Linux) или Docker Desktop с WSL2 (Windows). Проверка:

```bash
docker run --rm --gpus all nvidia/cuda:12.6.2-base-ubuntu22.04 nvidia-smi
```

При старте воркер пишет `InsightFace device: requested=gpu, active=gpu, ...`;
если там `active=cpu` — видеокарта контейнеру недоступна. Отчёты `[perf ...]`
подписаны устройством и ролью: `[perf worker gpu primary]`,
`[perf worker cpu overflow]`.

Без второго файла (`docker compose up`) всё работает как раньше — только на CPU.
