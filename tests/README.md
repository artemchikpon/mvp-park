# Тесты

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -v
```

Ничего поднимать не нужно — ни `docker-compose up`, ни Postgres, ни Redis:

- **БД** — временный sqlite-файл на сессию (создаётся в `tests/conftest.py`),
  схема пересоздаётся перед каждым тестом (`db_session` fixture).
- **Redis** — `fakeredis` вместо реального сервера (`fake_redis` fixture в
  `tests/test_worker.py`).
- **insightface/onnxruntime** — не устанавливаются и не импортируются;
  `tests/conftest.py` подменяет модуль `face_engine` лёгкой заглушкой ещё
  до первого импорта модулей воркера.
- **HR API** — сетевые функции `employee_sync` подменяются через monkeypatch.

## Что покрыто

| Файл | Что тестирует |
|---|---|
| `test_stats_service.py` | Бизнес-логика аналитики: Live Occupancy, графики день/неделя/месяц, демография, группировка по возрасту, санитизация некорректных age/gender, ежедневный сброс галереи лиц (`reset_persons`) |
| `test_auth.py` | без токена — 401 на всех бизнес-роутах; схема OAuth2 в OpenAPI |
| `test_jwt_auth.py` | JWT: пароли, токены, вход/refresh, роли admin/viewer, `/users`, первый admin |
| `test_api_cameras_settings_persons.py` | CRUD камер, настройки парка, список/счётчик/ручной сброс (`POST /persons/reset`) уникальных лиц — через `TestClient` |
| `test_api_employees.py` | Роутер `/employees`: список, удаление, `sync`/`add` через Redis-очередь (успех, ошибка воркера → 422/500/502, таймаут → 504) и обработка этих задач на стороне воркера (`employee_jobs`) |
| `test_employee_sync.py` | Синхронизация с HR: добавление/обновление по `external_id`, подсчёт skipped/errors, тексты ошибок, на которые опирается API |
| `test_worker.py` | Cooldown-дедупликация пересечений (`crossings`), кэш камер (`camera_cache`), сквозная обработка кадра `process_frame` (кадр из Redis → событие в БД), ежедневный сброс `persons` (`persons_reset`, Redis-лок против двойного удаления двумя репликами) |
| `test_producer.py` | Управление потоками камер: старт новых, остановка удалённых, перезапуск при смене URL или падении потока |
| `test_face_storage.py` | Сопоставление лиц по cosine similarity, фильтрация сотрудников, выбор лучшего совпадения при нескольких похожих |
| `test_schemas.py` | Pydantic-валидаторы (`CameraCreate`, `ParkSettingsUpdate`) |

## Чего здесь нет (и почему)

- **Захват RTSP** (`producer/camera.py: stream_camera`) — тонкая обёртка над
  `cv2.VideoCapture`; без реальной камеры тестировать особо нечего.
- **HTTP-уровень HR API** (`_fetch_employees`, `_fetch_photo_as_frame`) —
  для покрытия нужен `respx`/`httpx_mock`.
- **Реальный consumer-group flow воркера** (`stream_consumer`, `worker.main()`) — это
  бесконечные циклы с реальными сетевыми вызовами к Redis; их лучше
  проверять integration-тестом с поднятым `docker-compose` (или
  `testcontainers`), а не юнит-тестом.
