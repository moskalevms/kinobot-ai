# src/db_bootstrap.py
"""Идемпотентное создание схемы БД через `db.create_all()` (бэклог T1, баг 2).

СТАТУС С T12 — LEGACY/FALLBACK: штатный путь подготовки схемы при старте бота —
Alembic-миграции (`db_migrations.apply_database_migrations()`, вызывается из
`main()` в `src/telegram_bot.py`); этот create_all-хук из `main()` больше не
вызывается. Модуль СОХРАНЁН осознанно: от него зависят
`migrations/env.py` и `db_migrations.py` (константы `DEFAULT_DATABASE_URL`,
`CONNECT_TIMEOUT_SECONDS` — единый источник) и регрессионные тесты T1/T4;
хук применим вручную как аварийный fallback, если миграции недоступны.

Корень бага 2: таблицы `movie_feedback`, `watchlist`, `user_statistics`,
`offtopic_refusals`, `rt_scores` создавались только ручным
`python init_db.py` — Dockerfile CMD и deploy.yml их не создают, поэтому на
свежем деплое кнопки «Не моё»/«Смотрел»/«Статистика»/оценки молча не
работали: менеджеры fail-silent-или (False / available=False).

Решение — стартап-хук `ensure_database_schema()`: `db.create_all()` через
минимальное Flask-приложение (сложившийся паттерн
`FeedbackManager._create_minimal_app` / `WatchlistManager` /
`session_manager`). Единственный источник схемы — модели
`src/models/database.py`; третий источник (ручной SQL, дублирующие
определения) НЕ создаётся. `create_all()` идемпотентен: существующие
таблицы не пересоздаются, данные не теряются.

Сбой (БД недоступна, неверный URL, ошибка драйвера) — fail-silent:
исключение НЕ пробрасывается, пишется ERROR-лог, возвращается False —
бот ОБЯЗАН стартовать даже без БД (подборки фильмов от БД не зависят).
Для postgresql-URL время ожидания коннекта ограничено
(`connect_timeout`, psycopg2), чтобы недоступный сервер не блокировал
старт на системный таймаут TCP; SQLite параметр не поддерживается —
для не-postgresql URL engine options не задаются.

ВРЕМЕННОЕ решение задачи T1: точка вызова хука — `main()` в
`src/telegram_bot.py` — была оформлена под замену, и в T12 замена выполнена:
в той же точке (до сборки приложения PTB) вызывается
`db_migrations.apply_database_migrations()` (`alembic upgrade head`),
остальной код стартапа не изменился.

Отклонённая альтернатива — реиспользовать `init_database()` из init_db.py:
она требует ADMIN_PASSWORD (sys.exit(1) без него), печатает эмодзи
(падение в cp1251-консоли без PYTHONIOENCODING) и создаёт админа —
лишние побочные эффекты для стартап-хука бота.
"""
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Дефолт — конвенция модулей БД бота (feedback_manager.py, stats_manager.py,
# watchlist_manager.py, session_manager.py): каждый модуль держит свою копию.
DEFAULT_DATABASE_URL = 'postgresql://postgres:postgres@localhost:5432/kinobot_db'

# Таймаут коннекта (секунды) для postgresql: недоступный сервер не должен
# блокировать старт бота надолго (design.md D3 изменения).
CONNECT_TIMEOUT_SECONDS = 5


def _create_minimal_app(database_url: str) -> Any:
    """Минимальное Flask-приложение для доступа к БД (режим бота).

    Копия паттерна `FeedbackManager._create_minimal_app`: DATABASE_URL,
    TRACK_MODIFICATIONS выключен, модели регистрируются общим расширением
    db (единственный источник схемы — models.database).
    """
    from flask import Flask
    from models.database import db

    app = Flask('kinobot_bootstrap')
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    if database_url.startswith('postgres'):
        # psycopg2 поддерживает connect_timeout (секунды); sqlite3 такого
        # параметра не имеет (TypeError) и не нуждается в нём — локальный файл.
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
            'connect_args': {'connect_timeout': CONNECT_TIMEOUT_SECONDS},
        }
    db.init_app(app)
    return app


def ensure_database_schema() -> bool:
    """Идемпотентно создать схему БД (`db.create_all()`) — legacy/fallback.

    True — таблицы созданы либо уже существовали; False — сбой (любое
    исключение перехватывается, пишется ERROR-лог). Штатный путь с T12 —
    `db_migrations.apply_database_migrations()` (`alembic upgrade head`),
    который вызывается из `main()` telegram_bot.py в той же точке; этот хук
    сохранён для аварийного применения и регрессионных тестов T1/T4.
    """
    try:
        from models.database import db

        app = _create_minimal_app(os.getenv('DATABASE_URL', DEFAULT_DATABASE_URL))
        with app.app_context():
            try:
                db.create_all()
            finally:
                # Хук одноразовый: освобождаем соединения пула временного
                # приложения, чтобы в пуле не оставалось «висящее» соединение
                # с PostgreSQL на всё время жизни процесса бота. На пути сбоя
                # dispose — best-effort: engine мог не создаться (неверный URL),
                # повторное исключение не должно маскировать исходное.
                try:
                    db.engine.dispose()
                except Exception:
                    pass
        logger.info("Схема БД проверена/создана (db.create_all) — таблицы фидбека, списка и статистики доступны")
        return True
    except Exception as e:
        logger.error(
            f"Не удалось создать схему БД: {e} — бот продолжит работу без БД "
            "(кнопки фидбека/списка/статистики будут недоступны до появления БД)",
            exc_info=True,
        )
        return False
