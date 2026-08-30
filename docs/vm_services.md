# Сервисы на VPS и секреты окружения

Подтверждено: **2026-08-30** (первичная подготовка и первый деплой).

## Текущее состояние

- Продакшен — VPS с Ubuntu 24 (38.180.228.133), пользователь `kinobot`
  (без sudo, в группе `docker`).
- Развёрнутые сервисы (контейнеры):
  - `kinobot` — Telegram-бот (`python src/telegram_bot.py`), образ
    `kinobot-ai:<тег релиза>` собирается на самом VPS пайплайном
    GitHub Actions; веб/админка не деплоится (решается отдельно);
  - `kinobot_postgres` — PostgreSQL 15 (alpine), порт 5432 не
    публикуется, доступен только в docker-сети.
- Конфигурация: `~/kinobot/docker-compose.prod.yml` (копируется
  пайплайном из `deploy/docker-compose.prod.yml` при каждом деплое).
- Секреты: только на VPS в `~/kinobot/.env.production` (права 600);
  в репозитории их нет и пайплайн файл не трогает.
- Старая VM 176.108.252.72 выведена из использования (рабочих
  сервисов на ней не было, данных для миграции нет).

## Состав `.env.production`

Шаблон при первичной подготовке выводит `deploy/bootstrap_vps.sh`.
Обязательные переменные:

- `FLASK_ENV=production`;
- `FLASK_SECRET_KEY` — случайное значение (без него `app.py`
  не стартует, проверка встроена в код);
- `ADMIN_PASSWORD` — для создания админа через `init_db.py`
  (без него инициализация завершается ошибкой);
- `DB_PASSWORD` — пароль PostgreSQL (используется compose-файлом);
- ключи интеграций: `TELEGRAM_BOT_TOKEN`, `KINOPOISK_API_KEY`,
  `GIGACHAT_AUTH_KEY`.

Необязательные (есть значения по умолчанию в коде/шаблоне):
`GIGACHAT_BASE_URL`, `ENABLE_DEEPSEEK` + `DEEPSEEK_API_KEY` /
`DEEPSEEK_BASE_URL`, `MIN_VOTES_IMDB`, `MIN_VOTES_KP`, `CACHE_TTL`,
`LOG_LEVEL`, `BOT_MODE`, `DB_USER`, `DB_NAME`.

При утере файла значения восстанавливаются владельцем проекта
по этому списку; файл пересоздаётся вручную, права 600.
