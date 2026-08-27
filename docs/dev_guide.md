# Гайд разработчика: запуск и деплой

## Запуск локально

Внутри `src/` импорты плоские (без префикса пакета), поэтому запуск
только файлом, НЕ как модуль (`python -m src.*` падает с
ModuleNotFoundError):

```
# Бот (подключается к реальному Telegram)
python src\telegram_bot.py

# Веб: порт 5000, админка /admin/login
python src\app.py

# Инициализация БД (таблицы + админ из ADMIN_PASSWORD)
python init_db.py
```

Консоль в кодировке cp1251: добавляйте `PYTHONIOENCODING=utf-8`,
иначе вывод с эмодзи падает с UnicodeEncodeError.

Особенность: `app.py` делает `chdir` в `src/`, поэтому веб пишет логи
в `src/logs/`, а не в корневой `logs/` (том `app_logs` в деплое
смонтирован в `/app/logs` — не менять).

## Сборка и публикация образа

```
# 1. Собрать образ
docker build -t kinobot-ai .

# 2. Логин в реестр Cloud.ru
docker login kinobot-ai.cr.cloud.ru

# 3. Протегировать (тег поднимается вручную при релизе)
docker tag kinobot-ai kinobot-ai.cr.cloud.ru/beta/kinobot-ai:<тег>

# 4. Пуш
docker push kinobot-ai.cr.cloud.ru/beta/kinobot-ai:<тег>
```

## Деплой на VM

```
ssh user1@176.108.252.72

scp docker-compose.yml user1@176.108.252.72:~/kinobot-deploy/
scp .env.production user1@176.108.252.72:~/kinobot-deploy/

# на VM:
docker compose -f docker-compose.prod.yml up -d
```

`docker-compose.prod.yml` живёт на VM, в репо отсутствует. Перед
деплоем убедиться, что в окружении на VM заданы `ADMIN_PASSWORD`
и `FLASK_SECRET_KEY` (состав сервисов — в `docs/vm_services.md`).
