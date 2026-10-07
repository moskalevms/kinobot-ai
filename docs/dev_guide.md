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

# Инициализация БД (схема — миграциями, + админ из ADMIN_PASSWORD)
python init_db.py
```

Консоль в кодировке cp1251: добавляйте `PYTHONIOENCODING=utf-8`,
иначе вывод с эмодзи падает с UnicodeEncodeError.

Особенность: `app.py` делает `chdir` в `src/`, поэтому веб пишет логи
в `src/logs/`, а не в корневой `logs/` (том `app_logs` в деплое
смонтирован в `/app/logs` — не менять).

## Миграции БД

Версионирование схемы — Alembic (бэклог T11): конфигурация `alembic.ini`
(строку подключения НЕ хранит — URL берётся из переменной окружения
`DATABASE_URL`, fallback — локальный PostgreSQL), миграции в `migrations/`,
единственный источник схемы — модели `src/models/database.py`
(`db.metadata`). Команды выполняются из корня репозитория:

```
# Создать миграцию по изменениям моделей (autogenerate-диф)
alembic revision --autogenerate -m "краткое описание"

# Применить все миграции до последней
alembic upgrade head

# Пометить СУЩЕСТВУЮЩУЮ прод-БД без наката DDL — штатный путь для БД,
# созданной СТАРОЙ версией init_db.py (create_all); данные и таблицы не изменяются
alembic stamp head

# Текущая версия схемы и история ревизий
alembic current
alembic history
```

Замечания:

- Подключение: без `DATABASE_URL` используется локальный
  `postgresql://postgres:postgres@localhost:5432/kinobot_db`
  (`src/db_bootstrap.DEFAULT_DATABASE_URL`).
- Windows: alembic читает `alembic.ini` в кодировке локали (cp1251), а файл
  хранится в UTF-8 (русские комментарии) — запускайте команды с
  `PYTHONUTF8=1`. В Linux (прод-контейнер) UTF-8 читается по умолчанию,
  переменная не нужна. Стартап-хелпер бота (`src/db_migrations.py`) читает ini
  ЯВНО в UTF-8, поэтому `PYTHONUTF8` для автоприменения миграций не требуется.
  В Docker-образ добавлен `ENV PYTHONUTF8=1` — страховка для CLI-запуска
  `alembic upgrade head` внутри контейнера (шаг деплоя) на случай смены
  базового образа или локали.
- Baseline `0001_initial` идемпотентна: каждый `create_table`/`create_index`
  проверяет существование объекта через инспектора БД и пропускает создание,
  если объект уже есть — накат безопасен и для частично созданной схемы.
- Ограничение offline: guard-ы baseline (`sa.inspect`) работают только online —
  `alembic upgrade head --sql` через `0001_initial` невозможен
  (NoInspectionAvailable); offline-генерация SQL — только для ревизий без guard-ов.
- Политика сохранности данных (additive-first; `drop_table`/`drop_column`/
  изменение типа — только через ручное ревью и явный бэкап БД) —
  `migrations/README.md`.
- Автоприменение миграций при старте бота и в деплое — см. подраздел ниже
  (реализовано задачей T12); регрессионные тесты миграций — задача T13;
  CLI-инициализатор `init_db.py` — тонкая обёртка (миграции + админ),
  см. подраздел T14 ниже.

### Автоприменение при старте и в деплое (T12)

Штатный путь схемы: старт бота (`python src/telegram_bot.py`, контейнер
`kinobot`) автоматически накатывает `alembic upgrade head` — хелпер
`src/db_migrations.apply_database_migrations()` вызывается в `main()` до сборки
приложения PTB. Поведение:

- идемпотентно: повторный старт — no-op (версия схемы хранится в служебной
  таблице `alembic_version`, данные не трогаются);
- fail-silent: при недоступной БД или ошибке ревизии пишется ERROR-лог и бот
  стартует без БД (подборки фильмов от БД не зависят) — для postgresql время
  ожидания коннекта ограничено `CONNECT_TIMEOUT_SECONDS`;
- аварийное отключение: `RUN_MIGRATIONS=false` (также `0`/`no`) в окружении бота
  (на проде — строка в `~/kinobot/.env.production`) — миграции пропускаются
  с INFO-логом, ошибкой это не считается;
- ЕДИНСТВЕННЫЙ раннер миграций — бот: веб-процесс (`src/app.py`, контейнер
  `kinobot-web`) не вызывает ни `upgrade`, ни `create_all` — гонка DDL между
  контейнерами исключена (комментарий у сервиса `kinobot` в
  `deploy/docker-compose.prod.yml`);
- файлы миграций доставляются в образ (`Dockerfile`: `COPY migrations/`,
  `COPY alembic.ini`), поэтому команды alembic внутри контейнера работают.

Порядок шагов пайплайна деплоя (`.github/workflows/deploy.yml`):

1. «Резервное копирование БД (pg_dump)» — ДО запуска контейнеров:
   `docker exec kinobot_postgres pg_dump -U postgres kinobot_db | gzip >`
   `~/kinobot/backups/kinobot_db_<дата_время>.sql.gz`, ротация — архивы старше
   7 суток удаляются; сбой бэкапа прерывает деплой (накатывать схему без копии
   недопустимо). Если контейнера `kinobot_postgres` нет или он не запущен
   (первичный деплой на свежий VPS) — шаг логирует предупреждение и пропускает
   бэкап. Учётные данные в шаге — дефолты compose (`DB_USER=postgres`,
   `DB_NAME=kinobot_db`): при иных значениях в `.env.production` команду шага
   правит оператор.
2. «Запуск контейнеров» — `docker compose up -d` (бот при старте сам накатывает
   миграции).
3. «Применение миграций БД (alembic upgrade head)» — контрольный прогон
   `docker exec kinobot alembic upgrade head`; шаг ФАТАЛЬНЫЙ (без `|| true`):
   при сбое деплой падает, оператор откатывает релиз.
4. «Создание/проверка админа БД (init_db.py)» — `docker exec kinobot python
   init_db.py` (тонкая обёртка с T14: повторный прогон `upgrade head` через
   хелпер — no-op после шага 3 — плюс идемпотентный админ из
   `ADMIN_PASSWORD`); шаг НЕфатальный — сбой (в т.ч. exit 1 при недоступной
   БД) логируется предупреждением.
5. «Проверка результата деплоя» — статусы контейнеров и `GET /health`.

Первичное внедрение на прод (один раз, для БД, созданной СТАРОЙ версией
`init_db.py` через `create_all`): пометить текущую схему как актуальную БЕЗ
наката DDL —

```
docker exec kinobot alembic stamp head
```

после чего все последующие деплои накатывают `upgrade head` автоматически.

Восстановление из бэкапа (при повреждении данных):

```
gunzip -c ~/kinobot/backups/kinobot_db_<файл>.sql.gz | \
  docker exec -i kinobot_postgres psql -U postgres kinobot_db
```

Откат и совместимость схемы:

- `alembic downgrade` на проде ЗАПРЕЩЁН (удаляет таблицы/колонки вместе с
  данными; baseline-`downgrade` существует только для локальных проверок);
- откат релиза (`workflow_dispatch` на старый тег) схему НЕ откатывает —
  совместимость «старый код + новая схема» обеспечивает политика
  «схема обратно совместима на один релиз»: миграции additive-first (новые
  таблицы/колонки/индексы), разрушающие изменения — только через ручное ревью
  и отдельную миграцию с явным бэкапом (`migrations/README.md`).

### init_db.py — тонкая обёртка: миграции + админ (T14)

`python init_db.py` — CLI-инициализатор, а НЕ второй источник DDL (дубль
схемы `db.create_all()` устранён задачей T14):

- схема накатывается только миграциями: скрипт вызывает хелпер
  `src/db_migrations.apply_database_migrations()` (`alembic upgrade head`);
  единственная точка эволюции схемы — `migrations/`, источник моделей —
  `src/models/database.py`;
- семантика CLI — fail-loud (в отличие от fail-silent старта бота): если
  миграции не применились (БД недоступна, файлы `alembic.ini`/`migrations/`
  не найдены, ошибка ревизии), скрипт печатает ошибку и завершается с кодом 1
  — админ НЕ создаётся; шаг деплоя выполняет команду с `|| true`, поэтому
  пайплайну ненулевой код не вредит;
- при успехе — идемпотентное создание роли 'admin' и пользователя 'admin'
  (`ADMIN_PASSWORD` обязателен: без него сообщение и код 1); повторный прогон
  ничего не пересоздаёт;
- повторный запуск на БД с накатанными миграциями безопасен: `upgrade head` —
  no-op, данные и версия схемы сохраняются (в т.ч. шаг 4 пайплайна деплоя
  после шага 3);
- аварийный выключатель оператора `RUN_MIGRATIONS=false` уважается: хелпер
  возвращает успех БЕЗ наката и без подключения к БД — скрипт продолжает
  создание/проверку админа, схема не изменяется;
- команда остаётся валидной без аргументов и локально (`python init_db.py`),
  и в контейнере (`docker exec kinobot python init_db.py`).

## Продакшен

Продакшен — VPS с Ubuntu 24 (38.180.228.133), два контейнера
приложения (бот и веб) + PostgreSQL в docker-сети. Конфигурация
`deploy/docker-compose.prod.yml` лежит в репозитории и копируется
на VPS при каждом деплое; `.env.production` на VPS (права 600)
записывает пайплайн из секрета GitHub `ENV_PRODUCTION` — в репозитории
секретов нет. Реестр образов не используется: образ `kinobot-ai:<тег>`
собирается на самом VPS.
Корневой `docker-compose.yml` (с образом из реестра) — только для локальной разработки, в продакшене не используется.

Контейнеры:

- `kinobot` — Telegram-бот (`python src/telegram_bot.py`);
- `kinobot-web` — веб-интерфейс и админ-панель (`gunicorn` с одним
  воркером), порт 5000 опубликован, доступен по `http://<IP>:5000`.

### Релиз (автодеплой)

Пайплайн GitHub Actions (`.github/workflows/deploy.yml`) запускается
пушем тега:

```
git tag v1.2.0
git push origin v1.2.0
```

Пайплайн: доставляет исходники на VPS (rsync), собирает образ,
записывает `.env.production` из секрета `ENV_PRODUCTION`, запускает
контейнеры и проверяет, что оба контейнера (бот и веб) работают без
перезапусков и веб-приложение отвечает на `/health`. Ход деплоя виден
во вкладке Actions.

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
- `VPS_PORT` — `22`;
- `ENV_PRODUCTION` — полное содержимое `.env.production` (включая
  токен бота); пайплайн записывает его на VPS при каждом деплое.

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

Перед ручным деплоем убедитесь, что на VPS уже есть корректный
`~/kinobot/.env.production` (пайплайн его не создаёт — в ручном режиме
файл правится вручную, права 600).

```
rsync -az --delete --exclude '.git' --exclude '.venv' --exclude '.env*' \
  --exclude logs --exclude src/logs --exclude data --exclude docs \
  --exclude openspec --exclude __pycache__ \
  ./ kinobot@38.180.228.133:~/kinobot/app/

scp deploy/docker-compose.prod.yml kinobot@38.180.228.133:~/kinobot/

ssh kinobot@38.180.228.133 'cd ~/kinobot/app && docker build -t kinobot-ai:v1.2.0 . && \
  cd ~/kinobot && APP_VERSION=v1.2.0 docker compose \
  --env-file .env.production -f docker-compose.prod.yml up -d'

# проверка: оба контейнера работают, веб отвечает
ssh kinobot@38.180.228.133 'docker ps && curl -s http://127.0.0.1:5000/health'
```

### Замечания

- Перед первым деплоем убедиться, что бот с этим токеном не запущен
  в другом месте (два polling-экземпляра конфликтуют).
- Деплой пересоздаёт контейнеры бота и веба — кратковременный простой.
- Образы старых тегов на VPS не удаляются (нужны для отката);
  чистятся только висячие (`docker image prune -f` в пайплайне).
- Порт 5432 наружу не публикуется — PostgreSQL доступен только
  в docker-сети.
