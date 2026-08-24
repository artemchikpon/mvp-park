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
  до первого импорта `worker.py`/`employee_sync.py`.

## Что покрыто

| Файл | Что тестирует |
|---|---|
| `test_stats_service.py` | Бизнес-логика аналитики: Live Occupancy, графики день/неделя/месяц, демография, группировка по возрасту, санитизация некорректных age/gender, ежедневный сброс галереи лиц (`reset_persons`) |
| `test_auth.py` | API-key авторизация (`require_api_key`) — юнит + через реальные роуты |
| `test_api_cameras_settings_persons.py` | CRUD камер, настройки парка, список/счётчик/ручной сброс (`POST /persons/reset`) уникальных лиц — через `TestClient` |
| `test_worker.py` | Cooldown-дедупликация пересечений, кэш камер, сквозная обработка кадра `_process_entry` (кадр из Redis → событие в БД), ежедневный сброс `persons` (`_persons_reset_loop` и Redis-лок против двойного удаления двумя репликами) |
| `test_face_storage.py` | Сопоставление лиц по cosine similarity, фильтрация сотрудников, выбор лучшего совпадения при нескольких похожих |
| `test_schemas.py` | Pydantic-валидаторы (`CameraCreate`, `ParkSettingsUpdate`) |

## Чего здесь нет (и почему)

- **producer/camera.py** — по сути тонкая обёртка над `cv2.VideoCapture` в
  бесконечном цикле; юнит-тестировать особо нечего без реальной RTSP-камеры
  или сложного мокания OpenCV. Если появится логика троттлинга FPS
  (см. список проблем проекта) — стоит добавить тесты на неё отдельно.
- **employee_sync.py** — сетевые вызовы к внешнему HR API; для нормального
  покрытия нужен `respx`/`httpx_mock` и более детальные фикстуры под формат
  ответа HR API. Можно добавить отдельным заходом.
- **Реальный consumer-group flow воркера** (`_main_loop`, `main()`) — это
  бесконечные циклы с реальными сетевыми вызовами к Redis; их лучше
  проверять integration-тестом с поднятым `docker-compose` (или
  `testcontainers`), а не юнит-тестом.
