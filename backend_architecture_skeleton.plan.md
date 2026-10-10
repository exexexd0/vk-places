# Скелет бэкенда vk-places
Фронтенд (VK Mini App, VKUI, карта) в этот репозиторий не входит. Единственная точка стыка — api/openapi.yaml: POST /api/v1/routes/generate и GET /api/v1/routes/{id}.
Язык монолита — Go. ETL — отдельная команда того же модуля, не рантайм API. Опциональные фичи из документа (VRPTW, Redis, worker pool, текстовые сноски) остаются швами в интерфейсах, без реализации.

# Поток генерации
Выбор мест — метод якорной точки из architecture.md (LLM возвращает категории, PostGIS KNN собирает 3 кучных варианта). Гибридный RAG (pgvector) — второй PlaceSelector, подключается позже тем же портом. Итоговая геометрия — OSRM, не Haversine.
flowchart LR
  client[FrontendRepo] --> api[HTTP_API]
  api --> orch[route.Service]
  orch --> llm[llm.Engine]
  orch --> poi[poi.Selector]
  orch --> osrm[routing.Engine]
  poi --> pg[(Postgres_PostGIS_pgvector)]
  llm --> llmApi[LLM_API]
  osrm --> osrmBox[OSRM]
  etl[cmd_etl] --> pg
route.Service — единственный оркестратор. Пакеты poi, llm, routing не импортируют друг друга, только свои порты.

# Дерево репозитория
- cmd/api/main.go — HTTP-сервер, сборка зависимостей.
- cmd/etl/main.go — офлайн-пайплайн по Иваново_best.json.
- internal/platform — config из env, chi-роутер, pgx-пул, health.
- internal/route — use case Generate: таймаут LLM, фоллбэк на топ по рейтингу, сохранение маршрута.
- internal/poi — домен места, репозиторий, AnchorSelector (якорь = самая редкая категория, топ-3 по рейтингу, KNN <-> по остальным).
- internal/llm — порт Engine и OpenAI-совместимый клиент (LLM_BASE_URL + LLM_API_KEY + LLM_MODEL). Structured output: categories, transport_mode, опциональный order.
- internal/routing — клиент OSRM table/route и Held-Karp на N <= 10.
- internal/etl — стадии пайплайна отдельными функциями.
- migrations/001_places.sql — схема.
- deploy/docker-compose.yml — Postgres и OSRM.
- api/openapi.yaml — контракт для фронта.
- .env.example, Makefile — make up, make api, make etl.

# Схема данных
Таблица places: id, org_id, city, name, address, category, tags text[], opening_hours jsonb, avg_visit_duration_min, rating, location geography(Point,4326), embedding vector(1536), GiST по location, HNSW по embedding.
Таблица routes: id uuid, city, title, transport_mode, total_distance_meters, total_duration_minutes, waypoints jsonb, route_geometry text, created_at. Нужна, чтобы фронт открывал маршрут по route_id (шаринг).
Сырой JSON не содержит координат и категории — только name (с приклеенными рейтингом и статусом), address, rating, comments, org_id. ETL поэтому включает геокодер и классификатор, а не только regexp.

# ETL-стадии
1. Разбор JSON.
2. Очистка имени: отрезать хвост с оценкой и «Закрыто до…».
3. Фильтр по whitelist из документа (еда, культура, парки, достопримечательности, развлечения, крупные ТЦ, вокзалы как якоря старта/финиша, спорт). Чёрный список: бухгалтерия, лизинг, автосервис, склады.
4. Теги из отзывов — порт Summarizer (заглушка: пустой список, пока нет ключа LLM).
5. Геокодинг адреса — порт Geocoder, адаптер Nominatim (тот же OSM, что у OSRM). Без ключа строка пропускается с логом.
6. Эмбеддинг описания+тегов — порт Embedder, no-op в скелете (embedding nullable).
7. Upsert в places по org_id.

# HTTP-контракт
POST /api/v1/routes/generate
Запрос: city, prompt, transport_mode (walk | car | transit). transit в скелете принимается и мапится на пешеходный профиль OSRM с пометкой в ответе — отдельного движка ОТ нет.
Ответ: route_id, route_title, transport_mode, total_distance_meters, total_duration_minutes, waypoints[] (order, place_id, name, lat, lon, category, suggested_time), route_geometry (encoded polyline OSRM).
GET /api/v1/routes/{id} — тот же объект. GET /health.
Скелетные адаптеры LLM и OSRM возвращают контролируемую ошибку, если сервис не поднят; оркестратор при таймауте LLM (3.5 с) зовёт rule-based выборку. Компиляция и GET /health работают без ключей и без графа OSRM.

# Docker
- Postgres: свой Dockerfile на postgis/postgis с расширением pgvector, CREATE EXTENSION postgis, vector.
- OSRM: сервис с профилем foot и томом data/osrm. В Makefile цель osrm-prepare с шагами Geofabrik PBF (Ивановская область / ЦФО) и osrm-extract / osrm-partition / osrm-customize. Сам PBF в репозиторий не кладётся.
Что сознательно не делаем в скелете
Реализацию VRPTW, Redis-кэш матриц, пул воркеров, текстовые сноски LLM, боевой pgvector-retrieval и любой код фронтенда. Порт PlaceSelector и поле opening_hours оставляют место для этих шагов без смены API.

# Предобработка
## Временная на моковых данных
у всех 6481 мест в Иваново_best.json теперь есть поле type со значением null. Остальные поля не менялись. Повторный запуск скрипта файл не трогает и не затирает уже заполненный type.

Скрипт — scripts/add_null_type.py. Образ описан в корневом Dockerfile (python:3.12-slim). Собрать его отсюда нельзя: Docker в этом WSL-дистрибутиве не подключён. Когда интеграция с Docker Desktop будет включена:

docker build -t vk-places-etl .
docker run --rm -v "$PWD:/data" -w /data vk-places-etl Иваново_best.json

## Постоянная
