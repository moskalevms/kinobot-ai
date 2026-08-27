# Текущее состояние проекта

> **Обновление от 2026-08-27** (изменение `project-stabilization`):
> снимок ниже частично устарел. С момента снимка:
> - переход на плоские импорты завершён, `Dockerfile` CMD —
>   `python src/telegram_bot.py` (запуск с `-m` не работает);
> - удалены: `src/monitoring.py`, режим `BOT_MODE=webhook`
>   (`/telegram-webhook`, `setup_webhook`), легаси `data/processed/`,
>   `src/models/movie.py`, `src/utils/code_collector.py`;
> - сессии диалога хранятся в PostgreSQL (`SessionManager` с фолбэком
>   на in-memory), кэш поиска изолирован по пользователям;
> - ретраи GigaChat/Kinopoisk — только временные ошибки (429/5xx);
> - единая инициализация БД — `init_db.py` (модуль моделей —
>   `src/models/database.py`, дубли удалены); `CURRENT_YEAR` — один
>   источник в `src/config.py`;
> - добавлены тесты (`tests/`, pytest), ruff, mypy, CI GitHub Actions;
> - безопасность: `ADMIN_PASSWORD` обязателен для `init_db.py`,
>   `FLASK_SECRET_KEY` обязателен при `FLASK_ENV=production`,
>   `debug=True` вне разработки не включается;
> - README переписан под реальное состояние, деплой-команды — в
>   `docs/dev_guide.md`.

Снимок от 2026-08-26. Сформирован по результатам обхода кодовой базы,
истории git (последний коммит `a09c7f0` + незакоммиченные изменения)
и проверок запуском в локальном окружении.

## 1. Назначение

Kinobot — рекомендатель фильмов и сериалов:

- Telegram-бот с живым диалогом (интенты, контекст сессии, кнопки);
- Flask веб-интерфейс (чат) + админ-панель со статистикой.

Данные о фильмах берутся из Kinopoisk API (api.kinopoisk.dev),
понимание пользовательских запросов — через LLM (GigaChat, запасной
DeepSeek). PostgreSQL используется только для админки и статистики.

## 2. Состав репозитория

```
kinobot-ai/
├── src/                        # весь код приложения
│   ├── telegram_bot.py         # Telegram-бот (polling + заготовки webhook)
│   ├── app.py                  # Flask: чат /chat, /health, webhook, админка
│   ├── dialogue_manager.py     # ядро диалога: интенты → поиск → ответ
│   ├── intent_classifier.py    # LLM-классификация + regex-fallback
│   ├── movie_agent.py          # фасад поиска + in-memory кэш (TTL 45 с)
│   ├── recommendation_engine.py# сбор кандидатов из Kinopoisk API
│   ├── kinopoisk_client.py     # HTTP-клиент (retry на 429/5xx)
│   ├── llm_router.py           # GigaChat → (опц.) DeepSeek
│   ├── gigachat_client.py      # OAuth + completions, SSL verify отключён
│   ├── session_manager.py      # in-memory сессии пользователей
│   ├── admin_routes.py         # админка: login/dashboard/statistics/users
│   ├── monitoring.py           # мёртвый код (сломан, не импортируется)
│   ├── models/database.py      # User, Role, UserStatistics
│   ├── prompts/                # промпт извлечения параметров (управляет
│   │                           # интентами и поведением без правки кода)
│   ├── templates/, static/     # веб-чат + шаблоны админки
│   └── utils/movie_filter.py   # фильтрация/ранжирование кандидатов
├── init_db.py                  # создание таблиц БД и админа (дубль моделей)
├── Dockerfile, docker-compose.yml
├── docs/                       # рабочие заметки (частично устарели)
├── data/processed/*.csv        # легаси, кодом не используется
└── openspec/                   # управление изменениями
```

Легаси (не используется кодом): `data/processed/`, `src/models/movie.py`,
`src/utils/code_collector.py` (дамп кода в файл на рабочем столе для
вставки в LLM).

## 3. Архитектура

```
                 ┌────────────────┐          ┌────────────────┐
   Telegram ────▶│ telegram_bot.py│          │  app.py (Flask)│◀── браузер /чат
   (polling)     └───────┬────────┘          └───────┬────────┘
                         │                           │
                         ▼                           ▼
                    ┌──────────────────────────────────┐
                    │  DialogueManager (общий для обоих)│
                    │  • сессии: SessionManager (in-mem)│
                    │  • интенты: IntentClassifier ─────┼──▶ LLMRouter ─▶ GigaChat
                    │    (initial/info/similar/alter.)  │      └────────▶ DeepSeek (опц.)
                    └───────────────┬──────────────────┘
                                    ▼
                    ┌──────────────────────────────────┐
                    │ MovieAgent (кэш 45 с, in-memory) │
                    │ └ RecommendationEngine           │
                    │    └ KinopoiskClient ────────────┼──▶ api.kinopoisk.dev
                    │    └ movie_filter (рейтинг/голоса│
                    │       /жанры/страны)             │
                    └──────────────────────────────────┘

   PostgreSQL ── только админка и статистика (users, roles, user_statistics)
```

### Запрос пользователя (путь данных)

1. Текст приходит в `telegram_bot.py` (или `/chat` в `app.py`).
2. `IntentClassifier` отправляет сообщение в LLM с промптом
   `parameter_extraction_prompt.txt`; ожидает JSON с полями
   `intent, genre, year, year_range, actor, director, country, mood,
   count, min_rating, movie_type, target_movie`. При ошибке LLM —
   regex-fallback (`_classify_fallback`).
3. `DialogueManager` по интенту выбирает ветку:
   - `initial`/прочие — подбор по параметрам (настроение раскладывается
     в список жанров словарём `mood_to_genre`);
   - `info` — поиск по названию, карточка фильма;
   - `similar` — похожие на последние рекомендации (жанр/год/рейтинг
     берутся из сессии);
   - `alternative` — «другие варианты» по параметрам последней выдачи.
4. `MovieAgent` проверяет кэш (ключ из параметров, TTL 45 с), затем
   `RecommendationEngine` запрашивает Kinopoisk API (до 250 кандидатов,
   для «топов» дополнительно список top250), `movie_filter` отсеивает
   нежелательные жанры, фильтрует по рейтингу/числу голосов, сортирует
   (вес рейтинга зависит от страны, для российского контента — КП).
5. Ответ формируется списком с inline-кнопками «Подробнее: <название>»
   (callback `info:<id>`), постер отправляется отдельным сообщением.

### Режимы запуска

| Режим | Команда | Примечание |
|---|---|---|
| Бот, polling | `python src\telegram_bot.py` | только плоские импорты |
| Веб + админка | `python src\app.py` | порт 5000, `/admin/login` |
| Веб + бот (webhook) | `BOT_MODE=webhook` в `.env` | заготовка, не проверена |
| Инициализация БД | `python init_db.py` | docker compose up -d postgres |
| Docker | `Dockerfile` CMD `python -m src.telegram_bot` | **несовместим с текущими импортами** |

### Деплой

Образ собирается локально, пушится в
`kinobot-ai.cr.cloud.ru/beta/kinobot-ai:<тег>`, затем `scp` на VM
(`user1@176.108.252.72:~/kinobot-deploy/`) и
`docker compose -f docker-compose.prod.yml up -d` (файл живёт на VM,
в репо отсутствует). `docker-compose.yml` в репо ссылается на тег 1.0.3.

## 4. Функционал

- Рекомендации фильмов и сериалов: по настроению, жанру, году /
  десятилетию / диапазону лет, актёру, режиссёру, стране.
- Топы (список top250 Kinopoisk) и «топ N».
- Карточка фильма по названию (интент `info`, команда `/movie`,
  кнопка «Подробнее»).
- «Похожие» и «другие варианты» по контексту последней выдачи.
- Клавиатурное меню бота, команды `/start /help /movie /top /genre /mood`.
- Веб-чат (те же возможности, без постеров/кнопок Telegram).
- Админ-панель: вход, дашборд, статистика по дням (уникальные
  пользователи, запросы), список пользователей.
- `/health` — проверочный запрос к Kinopoisk через движок рекомендаций.
- Ротация логов: 50 МБ на файл, хранение 4 дня.
- Кэширование выдачи (45 с), ретраи к внешним API, плавный фолбэк
  при недоступности LLM.

## 5. Текущие проблемы

### Критические — рабочая копия не работоспособна

| # | Проблема | Где |
|---|---|---|
| К1 | Незавершённый переход импортов: в последнем коммите импорты пакетные (`from .session_manager ...`), в рабочей копии — плоские. Оба способа запуска сломаны: `python -m src.telegram_bot` (CMD Dockerfile) падает на плоских импортах; `python src\telegram_bot.py` падает на К2 | все модули `src/` |
| К2 | Относительный импорт `from .utils.movie_filter import is_russian_content` в файле, переведённом на плоские импорты → **ImportError на каждой выдаче рекомендаций** (проверено запуском) | `recommendation_engine.py:271` |
| К3 | Незакоммиченная работа: админка + переход импортов, 18 файлов, +771 строка — риск потери | `git status` |

### Высокие

| # | Проблема | Где |
|---|---|---|
| В1 | In-memory сессии и кэш: теряются при рестарте, не разделяются между воркерами | `session_manager.py`, `movie_agent.py:18` |
| В2 | Кэш и флаг ошибки глобальны: «похожие»/неудачный поиск одного пользователя очищают кэш всем | `movie_agent.py:57-60,107` (`clear_cache` в `dialogue_manager.py`) |
| В3 | Нет тестов, линтера, тайпчекера, CI | — |
| В4 | Webhook-режим: update кладётся в очередь, но Application не запускается (нет `run_webhook`/updater) — режим, скорее всего, не работает | `app.py:69-75,181-188` |

### Средние и низкие

| # | Проблема | Где |
|---|---|---|
| С1 | Мёртвый сломанный модуль: `@app.route` без `app`, `prometheus_client` нет в requirements, нигде не импортируется | `monitoring.py` |
| С2 | Модели БД продублированы в трёх местах | `models/database.py`, `init_db.py`, flask-команда в `app.py:198-224` |
| С3 | Текущий год 2025 захардкожен минимум в 5 местах | `telegram_bot.py:31`, `dialogue_manager.py:146,149,307`, `movie_agent.py:50`, `movie_filter.py:89` |
| С4 | Retry GigaChat на все исключения (включая 401); функции определения временных ошибок не используются | `gigachat_client.py:65-71` |
| С5 | В `requirements.txt` gunicorn дважды, pandas/numpy/requests не используются; `MIN_VOTES_IMDB/MIN_VOTES_KP` из `config.py` никто не читает (пороги захардкожены в фильтре) | `requirements.txt`, `config.py`, `utils/movie_filter.py:91-101` |
| С6 | Логирование: корневой логгер + повторный `addHandler` (дубли строк), относительный `LOG_DIR` (веб пишет в `src/logs/`, бот в `logs/`) | `log_setup.py` |
| С7 | Безопасность: админ по умолчанию `admin/admin123`, пароль печатается в консоль, дефолтный secret key, `debug=True` при прямом запуске | `init_db.py:106-112`, `app.py:32,230` |
| С8 | Дублирование кода: регистрация хендлеров в двух функциях; логика «Подробнее» копирует `search_by_title` | `telegram_bot.py` (`main`/`create_telegram_app`, `handle_movie_detail`) |
| С9 | Интенты `newer`/`older` описаны в промпте, но не обрабатываются кодом | `prompts/parameter_extraction_prompt.txt`, `dialogue_manager.py:58-65` |
| С10 | Документация: README — черновик концепции, `docs/` устарел, `openspec/config.yaml` был пуст | репо в целом |

## 6. План исправления

### Фаза 0 — разблокировать (часы)

1. Закоммитить текущую работу над админкой отдельным коммитом.
2. Починить `recommendation_engine.py:271` — использовать импорт из
   верхней части файла (`from utils.movie_filter import ...`).
3. Завершить переход на плоские импорты: изменить CMD Dockerfile
   (например, `python src/telegram_bot.py`) — либо откатить импорты
   к пакетным и оставить `-m`. Выбрать один вариант и зафиксировать
   в документации.
4. Проверить запуск: бот отвечает, веб отдаёт `/health`.

### Фаза 1 — надёжность (1–2 недели)

1. Сессии и кэш: изолировать по пользователям как минимум в рамках
   процесса; для нескольких воркеров и переживания рестартов —
   вынести в Redis (или PostgreSQL).
2. Webhook: реализовать через `Application.run_webhook` либо удалить
   режим и заготовки.
3. Ретраи GigaChat/Kinopoisk — только временные ошибки (429/5xx, сеть).
4. `monitoring.py`: удалить либо реализовать `/metrics` через фабрику
   приложения и добавить `prometheus_client` в зависимости.

### Фаза 2 — качество (2–3 недели)

1. Тесты на ключевую логику: `dialogue_manager`, `intent_classifier`,
   `movie_filter` (без внешних API).
2. ruff + mypy, CI на тесты и сборку образа.
3. Единая инициализация БД (убрать дублирование моделей), миграции —
   например, alembic.
4. `current_year` — один источник (вычисляется из даты или из env).
5. Аудит `requirements.txt` и `config.py` (удалить неиспользуемое).
6. Именованные логгеры, абсолютные пути логов, единая точка настройки.

### Фаза 3 — гигиена и безопасность

1. Пароль админа — обязательный из env, без вывода в консоль;
   запрет запуска с дефолтным паролем в проде.
2. Убрать дефолтный secret key, `debug=True` вне разработки.
3. Переписать README под реальное состояние проекта, актуализировать
   `docs/`, удалить устаревшие заметки.
4. Удалить легаси: `data/processed/`, `models/movie.py`,
   `utils/code_collector.py`.
