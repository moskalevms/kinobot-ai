# migrations/env.py
"""Окружение Alembic проекта kinobot (бэклог T11).

URL подключения: переменная окружения DATABASE_URL; если она не задана —
локальный PostgreSQL (константа DEFAULT_DATABASE_URL из src/db_bootstrap.py —
единый источник дефолта, копия строки здесь НЕ заводится). Боевой URL в
alembic.ini намеренно отсутствует (заглушка).

Целевые метаданные: модели src/models/database.py (db.metadata) —
единственный источник схемы; дублирующих определений таблиц в миграциях нет.
Импорты в src/ плоские (образец — init_db.py:16), поэтому перед импортом
моделей в sys.path добавляется каталог <repo>/src; импорт после настройки
пути — ruff E402 для этого файла разрешён осознанно (per-file-ignores в
pyproject.toml, как для init_db.py).

ПОЛИТИКА СОХРАННОСТИ ДАННЫХ (подробно — migrations/README.md): миграции
additive-first — создают новые таблицы/колонки/индексы; разрушающие операции
(drop_table/drop_column/изменение типа) допускаются только через ручное
ревью и отдельную миграцию с явным бэкапом БД (см. T12).
"""
import logging
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import URL, make_url

# Плоские импорты src/: настройка пути ДО импорта моделей (init_db.py:16).
_SRC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src')
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)

from dotenv import load_dotenv

# .env в корне репозитория (если есть): override=False — переменная окружения,
# экспортированная в шелле, приоритетнее значения из файла.
load_dotenv()

from db_bootstrap import DEFAULT_DATABASE_URL
from models.database import db

logger = logging.getLogger('alembic.env')

# Объект Config Alembic: доступ к значениям alembic.ini.
config = context.config

# Настройка логирования из alembic.ini ([loggers]/[handlers]/[formatters]).
# Программный запуск из процесса бота (src/db_migrations.py, задача T12) передаёт
# атрибут configure_logger=False: fileConfig по умолчанию отключает существующие
# логгеры (disable_existing_loggers) и перенастраивает root-логгер — конфигурация
# логирования бота (log_setup.setup_logging) была бы потеряна. CLI-запуски
# (`alembic upgrade ...`) атрибут не задают — логирование настраивается как раньше.
if config.attributes.get('configure_logger', True) and config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Метаданные моделей src/models/database.py — цель сравнения autogenerate.
target_metadata = db.metadata


def _database_url() -> URL:
    """URL подключения: DATABASE_URL из окружения, fallback — локальный postgres."""
    raw_url = os.getenv('DATABASE_URL')
    if raw_url:
        logger.info('Миграции: используется DATABASE_URL из окружения')
    else:
        raw_url = DEFAULT_DATABASE_URL
        logger.info('Миграции: DATABASE_URL не задан — используется локальный PostgreSQL по умолчанию (db_bootstrap.DEFAULT_DATABASE_URL)')
    url = make_url(raw_url)
    # Пароль в лог не выводится (hide_password) — секреты не должны попадать в консоль.
    logger.info('Миграции: целевая БД — %s', url.render_as_string(hide_password=True))
    return url


def run_migrations_offline() -> None:
    """Offline-режим (--sql): генерация DDL-скрипта без подключения к БД.

    ОГРАНИЧЕНИЕ: guard-ы baseline-ревизии `0001_initial`
    (`sa.inspect(op.get_bind())`) требуют РЕАЛЬНОГО подключения и работают
    только в online-режиме. При `alembic upgrade head --sql` op.get_bind()
    возвращает MockConnection, инспекция невозможна (NoInspectionAvailable) —
    offline-генерация SQL через baseline не выполняется. Offline-режим
    применим только к последующим ревизиям без guard-ов.
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
        # render_as_batch=False: целевой диалект — PostgreSQL, ALTER TABLE
        # поддерживается нативно; пакетный режим (пересоздание таблиц)
        # не используется. SQLite применяется только для локальных проверок.
        render_as_batch=False,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Online-режим: подключение к БД и накат миграций.

    Подключение может быть передано извне через
    config.attributes['connection'] (рецепт программных запусков alembic —
    точка интеграции T12, используется `src/db_migrations.py`: engine с
    `connect_timeout` и NullPool создаёт хелпер и закрывает его сам);
    иначе engine создаётся из URL окружения с NullPool — соединения не
    остаются после завершения команды.
    """
    connectable = config.attributes.get('connection', None)
    if connectable is None:
        connectable = create_engine(_database_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # PostgreSQL — нативный ALTER, batch-режим не нужен (см. offline).
            render_as_batch=False,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
