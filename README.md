# Kinobot

Рекомендатель фильмов и сериалов: Telegram-бот с живым диалогом
и Flask веб-интерфейс (чат + админ-панель со статистикой).

- Данные о фильмах — Kinopoisk API (`api.kinopoisk.dev`).
- Понимание запросов — LLM: GigaChat (основной), запасной DeepSeek
  (только при `ENABLE_DEEPSEEK=true`).
- PostgreSQL используется только для админ-панели, статистики и
  сессий диалога.

## Стек

Python 3.10 (локально) / 3.11 (Docker), python-telegram-bot 20.x,
Flask 3, Flask-SQLAlchemy, Flask-Login, aiohttp, tenacity.

## Структура

```
kinobot-ai/
├── src/                        # весь код приложения
│   ├── telegram_bot.py         # Telegram-бот (только polling)
│   ├── app.py                  # Flask: чат /chat, /health, админка
│   ├── dialogue_manager.py     # ядро диалога: интенты → поиск → ответ
│   ├── intent_classifier.py    # LLM-классификация + regex-fallback
│   ├── movie_agent.py          # фасад поиска + кэш по пользователям
│   ├── recommendation_engine.py# сбор кандидатов из Kinopoisk API
│   ├── kinopoisk_client.py     # HTTP-клиент (ретраи только 429/5xx)
│   ├── llm_router.py           # GigaChat → (опц.) DeepSeek
│   ├── gigachat_client.py      # OAuth + completions (SSL verify отключён
│   │                           # намеренно — специфика API Сбера)
│   ├── session_manager.py      # сессии диалога в PostgreSQL,
│   │                           # фолбэк на in-memory
│   ├── admin_routes.py         # админка: вход/дашборд/статистика
│   ├── config.py               # конфиг + CURRENT_YEAR (один источник)
│   ├── log_setup.py            # идемпотентная настройка логирования
│   ├── models/database.py      # модели БД (единственное определение)
│   ├── prompts/                # промпты: поведение меняется без кода
│   ├── templates/, static/     # веб-чат + шаблоны админки
│   └── utils/movie_filter.py   # фильтрация/ранжирование кандидатов
├── init_db.py                  # единая инициализация БД и админа
├── tests/                      # pytest без внешних API
├── Dockerfile, docker-compose.yml
├── docs/                       # рабочие заметки: деплой, состояние
└── openspec/                   # управление изменениями
```

## Модель импортов и запуск

Внутри `src/` импорты плоские (без префикса пакета). Запуск только
файлом, НЕ как модуль:

- Бот: `python src\telegram_bot.py` (НЕ `python -m src.telegram_bot`)
- Веб (порт 5000, админка `/admin/login`): `python src\app.py`

`app.py` делает `chdir` в `src/`, поэтому веб-логи пишутся в
`src/logs/`, а не в корневой `logs/` — это поведение сохранено из-за
тома логов в деплое.

## Локальный запуск

1. Виртуальное окружение и зависимости:

   ```
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   .venv\Scripts\pip install -r requirements-dev.txt   # тесты/линтеры
   ```

2. Скопируйте `.env.example` в `.env` и заполните ключи:

   | Переменная | Назначение |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | токен бота |
   | `KINOPOISK_API_KEY` | Kinopoisk API (api.kinopoisk.dev) |
   | `GIGACHAT_AUTH_KEY` | GigaChat (обязателен, иначе старт падает) |
   | `DEEPSEEK_API_KEY` | запасная LLM (при `ENABLE_DEEPSEEK=true`) |
   | `DATABASE_URL` | PostgreSQL (по умолчанию — локальный) |
   | `ADMIN_PASSWORD` | пароль админа для `init_db.py` (обязателен) |
   | `FLASK_SECRET_KEY` | секрет сессий; обязателен при `FLASK_ENV=production` |
   | `LOG_DIR` | каталог логов (по умолчанию `logs`) |

3. PostgreSQL:

   ```
   docker compose up -d postgres
   ```

4. Создание таблиц и админ-учётки (пароль берётся из `ADMIN_PASSWORD`
   и никогда не печатается):

   ```
   python init_db.py
   ```

5. Запуск бота и/или веба (команды выше). Проверка веба:
   `GET http://localhost:5000/health` → `200 healthy`.

Консоль с кодировкой cp1251: запускайте с `PYTHONIOENCODING=utf-8`,
иначе эмодзи в выводе валят его с `UnicodeEncodeError`.

## Тесты и линтеры

```
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check src tests init_db.py
.venv\Scripts\python -m mypy
```

CI — GitHub Actions (`.github/workflows/ci.yml`): ruff, mypy, pytest,
сборка Docker-образа.

## Деплой

Образ собирается локально и пушится в
`kinobot-ai.cr.cloud.ru/beta/kinobot-ai:<тег>`, затем копируется на
VM и поднимается через `docker compose -f docker-compose.prod.yml up -d`
(compose-файл продакшена живёт на VM, в репо отсутствует).
Порядок команд — в `docs/dev_guide.md`. Тег версии в
`docker-compose.yml` поднимается вручную при релизе.

Перед деплоем убедиться, что в окружении на VM заданы
`ADMIN_PASSWORD` и `FLASK_SECRET_KEY` (см. `docs/`).

## Особенности и ограничения

- Интенты (`initial` / `info` / `similar` / `alternative`) и извлечение
  параметров управляются текстовыми промптами в `src/prompts/` —
  поведение меняется без правки кода.
- Кэш поиска (по умолчанию 45 с, `CACHE_TTL`) живёт в памяти процесса;
  сессии диалога — в PostgreSQL с фолбэком на память при ошибке БД.
- GigaChat: проверка SSL-сертификатов отключена намеренно
  (специфика API), не «чинить».
