# src/db_migrations.py
"""Автоприменение Alembic-миграций при старте бота (бэклог T12).

Штатный путь подготовки схемы БД: `main()` в `src/telegram_bot.py` вызывает
`apply_database_migrations()`, которая программно выполняет
`alembic.command.upgrade(cfg, 'head')` (ревизии — `migrations/`, конфигурация —
`alembic.ini` в КОРНЕ проекта). Легаси-хук `db_bootstrap.ensure_database_schema()`
(`db.create_all()`, задача T1) из `main()` больше не вызывается и сохранён как
fallback.

Ключевые решения (design.md изменения add-migrations-startup-deploy-t12):

- D1: путь к `alembic.ini` строится от `PROJECT_ROOT` (родительский каталог
  `src/`) и НЕ зависит от текущего рабочего каталога — работает и локально
  (`python src/telegram_bot.py` из корня), и в контейнере (`/app/src` → `/app`);
  `alembic.ini` использует `script_location = %(here)s/migrations`, поэтому
  каталог ревизий тоже разрешается от пути ini-файла. Отсутствие `alembic.ini`
  или `migrations/` проверяется ДО обращения к БД (отдельная ветка fail-silent).
- D2: engine создаёт сам хелпер и передаёт через `cfg.attributes['connection']`
  (точка интеграции `migrations/env.py`) — так применяются `connect_timeout`
  для postgresql (недоступный сервер не блокирует старт на системный TCP-таймаут)
  и NullPool (соединения не остаются после одноразового прогона); закрытие —
  `engine.dispose()` в finally, на пути сбоя best-effort. `alembic.ini` читается
  в UTF-8 явно — подкласс `Utf8AlembicConfig` переопределяет свойство
  `file_config` (а не в кодировке локали, как штатно делает alembic): иначе на
  Windows (cp1251) русские комментарии ini роняли бы чтение файла.
- D3: `cfg.attributes['configure_logger'] = False` — `migrations/env.py` при
  таком атрибуте НЕ вызывает `logging.config.fileConfig`, иначе настройки
  логирования из alembic.ini отключили бы существующие логгеры бота
  (`disable_existing_loggers`) и перенастроили root-логгер.
- D4: флаг окружения `RUN_MIGRATIONS` (`false`/`0`/`no`, регистронезависимо) —
  аварийный выключатель: намеренный пропуск с INFO-логом и возвратом True,
  БЕЗ подключения к БД (пропуск — осознанное действие оператора, не сбой).

Fail-silent сохраняется полностью (поведение T1): любое исключение —
ERROR-лог с трейсбеком и возврат False, бот ОБЯЗАН стартовать даже без БД
(подборки фильмов от БД не зависят). Единственный раннер миграций — бот:
`src/app.py` (контейнер `kinobot-web`) этот хелпер не вызывает.
"""
import logging
import os
from configparser import ConfigParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

# Базовый класс нужен в момент импорта модуля (наследование) — потому импорт
# на уровне модуля, а не ленивый: alembic — прод-зависимость (requirements.txt),
# sqlalchemy к моменту старта бота уже загружена менеджерами БД.
from alembic.config import Config

if TYPE_CHECKING:  # pragma: no cover - только аннотации типов
    from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# Корень проекта: родительский каталог для src/ (локально — репозиторий,
# в контейнере — /app). Модульная константа (а не вычисление внутри функции),
# чтобы тесты могли подменить путь (сценарий «нет alembic.ini»).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Значения RUN_MIGRATIONS, означающие намеренный пропуск миграций (D4).
_DISABLED_FLAGS = {'false', '0', 'no'}


def migrations_disabled() -> bool:
    """Нужно ли пропустить миграции (аварийный выключатель RUN_MIGRATIONS).

    `false`/`0`/`no` в любом регистре (пробелы по краям игнорируются) — пропуск;
    отсутствие переменной или любое иное значение — миграции выполняются.
    """
    return os.getenv('RUN_MIGRATIONS', '').strip().lower() in _DISABLED_FLAGS


class Utf8AlembicConfig(Config):
    """Config Alembic, читающий ini ЯВНО в UTF-8 (а не в кодировке локали).

    alembic читает ini через `configparser.read(..., encoding="locale")`:
    на Windows локаль — cp1251, а `alembic.ini` хранится в UTF-8 (русские
    комментарии) → UnicodeDecodeError, из-за которого миграции молча не
    применялись бы при локальном старте бота (CLI-запуск лечится `PYTHONUTF8=1`
    — см. docs/dev_guide.md, но стартап-хук на переменную окружения полагаться
    не может; в образ добавлен `ENV PYTHONUTF8=1` как страховка для CLI).

    Реализация — переопределение свойства `file_config` (публичный контракт
    `Config`), БЕЗ обращения к внутреннему механизму кеширования alembic/
    sqlalchemy `memoized_property`: собственное значение кэшируется в атрибуте
    экземпляра `_utf8_file_config`. Остальные параметры (`script_location` с
    `%(here)s`, `file_template`, `output_encoding`) alembic берёт из того же
    парсера; в Linux-контейнере (локаль UTF-8) поведение идентично штатному.
    """

    @property
    def file_config(self) -> ConfigParser:
        """ConfigParser содержимого ini, прочитанный в UTF-8 (кэш в экземпляре)."""
        cached = getattr(self, '_utf8_file_config', None)
        if cached is None:
            ini_path = Path(str(self.config_file_name))
            # `here` — каталог ini-файла (подстановка %(here)s в script_location):
            # путь абсолютный, поэтому каталог ревизий не зависит от cwd процесса.
            parser = ConfigParser({'here': ini_path.resolve().parent.as_posix()})
            parser.read([str(ini_path)], encoding='utf-8')
            self._utf8_file_config = parser
            cached = parser
        return cached


def _load_alembic_config(ini_path: str) -> Config:
    """Config Alembic для программного запуска из `alembic.ini` (UTF-8).

    Фабрика выделена, чтобы тесты могли проверить способ чтения конфигурации
    (см. `Utf8AlembicConfig`) отдельно от наката ревизий.
    """
    return Utf8AlembicConfig(ini_path)


def _create_migration_engine(database_url: str) -> 'Engine':
    """Одноразовый engine для наката миграций (NullPool + таймаут коннекта).

    `connect_timeout` поддерживается только psycopg2 (postgresql): для SQLite
    параметр не задаётся (sqlite3 не принимает такой kwarg — TypeError),
    локальный файл в таймауте не нуждается. Константы переиспользуются из
    `db_bootstrap` (единый источник дефолта и таймаута, копия не заводится).
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    from db_bootstrap import CONNECT_TIMEOUT_SECONDS

    options: Dict[str, Any] = {'poolclass': NullPool}
    if database_url.startswith('postgres'):
        options['connect_args'] = {'connect_timeout': CONNECT_TIMEOUT_SECONDS}
    return create_engine(database_url, **options)


def apply_database_migrations() -> bool:
    """Применить миграции схемы БД (`alembic upgrade head`) идемпотентно.

    True — миграции накатаны (либо схема уже в актуальной версии), либо пропуск
    был запрошен оператором через `RUN_MIGRATIONS`; False — сбой (файлы миграций
    не найдены, БД недоступна, ошибка ревизии): исключение НЕ пробрасывается,
    пишется ERROR-лог, бот продолжает старт без БД.
    """
    if migrations_disabled():
        # Возврат True: намеренный пропуск — не ошибка (D4). Подключения к БД нет.
        logger.info(
            "Применение миграций БД пропущено (RUN_MIGRATIONS=%s) — схема не изменяется",
            os.getenv('RUN_MIGRATIONS', ''),
        )
        return True

    engine: Optional['Engine'] = None
    try:
        from alembic import command

        ini_path = os.path.join(PROJECT_ROOT, 'alembic.ini')
        migrations_dir = os.path.join(PROJECT_ROOT, 'migrations')
        if not os.path.isfile(ini_path) or not os.path.isdir(migrations_dir):
            # Отдельная ветка fail-silent: файлов нет — обращаться к БД бессмысленно.
            logger.error(
                "Файлы миграций не найдены (alembic.ini=%s, migrations/=%s) — "
                "бот продолжит работу без применения схемы",
                ini_path,
                migrations_dir,
            )
            return False

        # Константы и импорты — внутри функции (паттерн T1: тяжёлые/опциональные
        # зависимости не поднимаются на импорте модуля бота).
        from db_bootstrap import DEFAULT_DATABASE_URL

        # `or`, а не второй аргумент getenv: пустая строка DATABASE_URL (например,
        # `DATABASE_URL=` в .env.production) не должна превращаться в
        # `create_engine('')`.
        database_url = os.getenv('DATABASE_URL') or DEFAULT_DATABASE_URL
        cfg = _load_alembic_config(ini_path)
        engine = _create_migration_engine(database_url)
        # Точка интеграции migrations/env.py: готовый engine вместо создания своего.
        cfg.attributes['connection'] = engine
        # Не переконфигурировать логирование процесса бота (D3).
        cfg.attributes['configure_logger'] = False

        command.upgrade(cfg, 'head')
        logger.info(
            "Миграции БД применены (alembic upgrade head) — таблицы фидбека, "
            "списка и статистики доступны"
        )
        return True
    except Exception as e:
        logger.error(
            f"Не удалось применить миграции БД: {e} — бот продолжит работу без БД "
            "(кнопки фидбека/списка/статистики будут недоступны до появления БД)",
            exc_info=True,
        )
        return False
    finally:
        # Прогон одноразовый: освобождаем соединения. На пути сбоя dispose —
        # best-effort: повторное исключение не должно маскировать исходное.
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass
