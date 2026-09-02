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

## Продакшен

Продакшен — VPS с Ubuntu 24 (38.180.228.133), один контейнер с ботом
+ PostgreSQL в docker-сети. Конфигурация `deploy/docker-compose.prod.yml`
лежит в репозитории и копируется на VPS при каждом деплое; секреты —
только на VPS в `~/kinobot/.env.production` (состав —
`docs/vm_services.md`). Реестр образов не используется: образ
`kinobot-ai:<тег>` собирается на самом VPS.

### Релиз (автодеплой)

Пайплайн GitHub Actions (`.github/workflows/deploy.yml`) запускается
пушем тега:

```
git tag v1.2.0
git push origin v1.2.0
```

Пайплайн: доставляет исходники на VPS (rsync), собирает образ,
запускает контейнеры и проверяет, что контейнер бота работает без
перезапусков. Ход деплоя виден во вкладке Actions.

### Ручной запуск и откат

Во вкладке Actions → «Деплой на VPS» → Run workflow укажите тег.
Так выполняется откат: запустите пайплайн с тегом предыдущего релиза
(его образ сохраняется на VPS).

### Секреты для пайплайна

В настройках репозитория GitHub (Settings → Secrets):

- `VPS_SSH_PRIVATE_KEY` — приватный ключ, выпущенный специально для
  Actions (см. подготовку ниже);
- `VPS_HOST` — `38.180.228.133`;
- `VPS_USER` — `kinobot`;
- `VPS_PORT` — `22`.

### Первичная подготовка VPS (один раз)

Перед подготовкой проверьте, что сервер соответствует требованиям к
аппаратным ресурсам под целевую нагрузку — расчёт и конфигурации в
[Installation-guide.md](Installation-guide.md).

1. Скопировать и выполнить скрипт подготовки (отдельно для каждого
   нового сервера):

   ```
   scp deploy/bootstrap_vps.sh root@38.180.228.133:
   ssh root@38.180.228.133 'bash bootstrap_vps.sh'
   ```

   Скрипт ставит Docker/Compose/rsync, создаёт пользователя `kinobot`
   и структуру `~/kinobot/`, выводит шаблон `.env.production`.

2. Заполнить секреты на VPS:

   ```
   ssh root@38.180.228.133 'nano /home/kinobot/kinobot/.env.production'
   ```

   Обязательные переменные: `FLASK_SECRET_KEY`, `ADMIN_PASSWORD`,
   `DB_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `KINOPOISK_API_KEY`,
   `GIGACHAT_AUTH_KEY`. Права файла — 600.

3. Выпустить ключ для GitHub Actions и прописать его на VPS:

   ```
   ssh-keygen -t ed25519 -f kinobot_actions_key -C "github-actions"
   # публичный ключ — на VPS:
   ssh root@38.180.228.133 'mkdir -p /home/kinobot/.ssh && \
     cat >> /home/kinobot/.ssh/authorized_keys' < kinobot_actions_key.pub
   # приватный ключ — в секрет VPS_SSH_PRIVATE_KEY на GitHub,
   # сам файл после этого удалить с машины.
   ```

4. Добавить секреты в репозиторий GitHub (список выше).

5. После первого успешного деплоя — инициализация БД (один раз):

   ```
   ssh kinobot@38.180.228.133 \
     'cd ~/kinobot && APP_VERSION=v1.2.0 docker compose \
      --env-file .env.production -f docker-compose.prod.yml \
      run --rm kinobot python init_db.py'
   ```

### Ручной деплой (если CI недоступен)

```
rsync -az --delete --exclude '.git' --exclude '.venv' --exclude '.env*' \
  --exclude logs --exclude src/logs --exclude data --exclude docs \
  --exclude openspec --exclude __pycache__ \
  ./ kinobot@38.180.228.133:~/kinobot/app/

scp deploy/docker-compose.prod.yml kinobot@38.180.228.133:~/kinobot/

ssh kinobot@38.180.228.133 'cd ~/kinobot/app && docker build -t kinobot-ai:v1.2.0 . && \
  cd ~/kinobot && APP_VERSION=v1.2.0 docker compose \
  --env-file .env.production -f docker-compose.prod.yml up -d'
```

### Замечания

- Перед первым деплоем убедиться, что бот с этим токеном не запущен
  в другом месте (два polling-экземпляра конфликтуют).
- Деплой пересоздаёт контейнер бота — кратковременный простой.
- Образы старых тегов на VPS не удаляются (нужны для отката);
  чистятся только висячие (`docker image prune -f` в пайплайне).
- Порт 5432 наружу не публикуется — PostgreSQL доступен только
  в docker-сети.
