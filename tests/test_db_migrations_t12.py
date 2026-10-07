# -*- coding: utf-8 -*-
"""Тесты T12: автоприменение Alembic-миграций при старте бота и в деплое.

Изменение add-migrations-startup-deploy-t12: штатный путь схемы БД —
`db_migrations.apply_database_migrations()` (программный `alembic upgrade
head`), вызывается из `main()` бота вместо легаси-хука T1
`db_bootstrap.ensure_database_schema()` (`db.create_all()`).

Проверяется:
- чистая БД → True, созданы все 8 таблиц моделей + служебная `alembic_version`;
- идемпотентность: повторный вызов — no-op, данные и версия схемы целы;
- fail-silent: недоступный postgres → False + ERROR-лог без исключения;
- отсутствие `alembic.ini`/`migrations/` → False + ERROR-лог БЕЗ обращения к БД;
- флаг `RUN_MIGRATIONS` (false/FALSE/0/no/No) → True + INFO-лог, подключения нет;
- менеджеры БД: до миграций fail-silent, после — данные пишутся/читаются;
- программный запуск не переконфигурирует логирование процесса
  (`configure_logger=False` + guard в migrations/env.py);
- текстовые guard'ы артефактов деплоя: Dockerfile, deploy.yml (порядок и
  фатальность шагов), compose-комментарий, `src/app.py` (веб не мигрирует),
  `docs/dev_guide.md` (инструкции оператору), guard в `migrations/env.py`.

Реальный PostgreSQL, docker и Telegram НЕ нужны: файловая SQLite в tmp_path
(паттерн tests/test_db_schema_bootstrap_t1.py) и чтение файлов репозитория.
Прогон — единицы секунд.
"""
import logging
import os
import re
from typing import Any, Dict, List

import pytest
import sqlalchemy as sa
from flask import Flask
from conftest import ROOT

import db_migrations
import db_bootstrap
from feedback_manager import FeedbackManager
from models.database import Watchlist, db
from stats_manager import StatsManager

# Все таблицы моделей src/models/database.py (единственный источник схемы) —
# baseline-ревизия 0001_initial создаёт их полностью.
MODEL_TABLES = {
    'users',
    'roles',
    'dialogue_sessions',
    'rt_scores',
    'watchlist',
    'movie_feedback',
    'offtopic_refusals',
    'user_statistics',
}

USER_ID = '777'
MOVIE_ID = 447301

# Недоступный сервер: порт 1 на localhost — немедленный connection refused
# (не блокирует прогон); connect_timeout хелпера страхует «молчащий» хост.
UNREACHABLE_URL = 'postgresql://u:p@127.0.0.1:1/kinobot_db'


@pytest.fixture()
def sqlite_url(tmp_path, monkeypatch):
    """URL файловой SQLite в tmp_path; DATABASE_URL подменён для хелпера.

    RUN_MIGRATIONS принудительно снят: тест проверяет штатное выполнение.
    """
    url = f"sqlite:///{(tmp_path / 'kinobot_t12.sqlite3').as_posix()}"
    monkeypatch.setenv('DATABASE_URL', url)
    monkeypatch.delenv('RUN_MIGRATIONS', raising=False)
    return url


def make_app(database_url: str) -> Any:
    """Минимальное приложение менеджеров (паттерн db_bootstrap._create_minimal_app).

    Таблицы НЕ создаёт — подключается к той же файловой БД, которую накатил
    (или не накатил) хелпер миграций.
    """
    app = Flask(f'test_t12_{id(database_url)}')
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


def table_names(url: str) -> set:
    """Имена таблиц БД через отдельный engine (sa.inspect) — без Flask-приложения."""
    engine = sa.create_engine(url)
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def alembic_version(url: str) -> str:
    """Текущая версия схемы из служебной таблицы `alembic_version`."""
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            row = connection.execute(sa.text('SELECT version_num FROM alembic_version')).first()
        return str(row[0]) if row else ''
    finally:
        engine.dispose()


def read_text(*parts: str) -> str:
    """Содержимое файла репозитория (текстовые guard'ы артефактов деплоя)."""
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as fh:
        return fh.read()


def deploy_step(text: str, name: str) -> str:
    """Блок шага deploy.yml по имени (для guard'ов порядка и фатальности).

    Границы шагов ищутся регулярным выражением по отступу (`^\\s+- name: `),
    а не жёсткой строкой с шестью пробелами — форматирование отступов в YAML
    может меняться, семантика guard'ов при этом сохраняется.
    """
    parts = re.split(r'^\s+- name: ', text, flags=re.MULTILINE)[1:]
    for block in parts:
        if block.splitlines()[0].strip() == name:
            return block
    raise AssertionError(f'шаг «{name}» не найден в deploy.yml')


def step_commands(block: str) -> str:
    """Исполняемые строки шага (без комментариев).

    Комментарии шагов намеренно содержат текст «без `|| true`» — guard'ы
    проверяют семантику команд, а не пояснения к ним.
    """
    lines = [ln for ln in block.splitlines() if not ln.strip().startswith('#')]
    return '\n'.join(lines)


# === 1. Чистая БД: upgrade head создаёт всю схему ===


def test_clean_db_upgrade_creates_all_tables(sqlite_url):
    """На чистой БД хелпер возвращает True и создаёт 8 таблиц + alembic_version."""
    assert db_migrations.apply_database_migrations() is True

    tables = table_names(sqlite_url)
    assert MODEL_TABLES <= tables, f'не созданы таблицы моделей: {MODEL_TABLES - tables}'
    assert 'alembic_version' in tables
    assert alembic_version(sqlite_url) == '0001'


# === 2. Идемпотентность: повторный вызов — no-op, данные целы ===


def test_repeat_run_is_noop_and_preserves_data(sqlite_url):
    """Второй прогон не падает, не делает лишнего DDL и не трогает строки данных."""
    assert db_migrations.apply_database_migrations() is True

    app = make_app(sqlite_url)
    with app.app_context():
        db.session.add(Watchlist(user_id=USER_ID, kinopoisk_id=MOVIE_ID, title='Начало'))
        db.session.commit()

    assert db_migrations.apply_database_migrations() is True

    with app.app_context():
        stored = db.session.query(Watchlist).filter_by(user_id=USER_ID).one()
        assert stored.kinopoisk_id == MOVIE_ID
        assert stored.title == 'Начало'
    assert alembic_version(sqlite_url) == '0001'


# === 3. Fail-silent: недоступная БД ===


def test_unreachable_db_fail_silent(monkeypatch, caplog):
    """Недоступный postgres: False без исключения + ERROR-лог."""
    monkeypatch.setenv('DATABASE_URL', UNREACHABLE_URL)
    monkeypatch.delenv('RUN_MIGRATIONS', raising=False)

    with caplog.at_level(logging.ERROR, logger='db_migrations'):
        result = db_migrations.apply_database_migrations()

    assert result is False
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, 'ожидался ERROR-лог о сбое применения миграций'
    assert 'Не удалось применить миграции БД' in errors[0].getMessage()


# === 4. Fail-silent: файлов миграций нет (без обращения к БД) ===


def test_missing_alembic_files_fail_silent_without_db(tmp_path, monkeypatch, caplog):
    """Пустой PROJECT_ROOT: False + ERROR-лог, engine НЕ создаётся."""
    monkeypatch.setattr(db_migrations, 'PROJECT_ROOT', str(tmp_path))
    monkeypatch.setenv('DATABASE_URL', UNREACHABLE_URL)
    monkeypatch.delenv('RUN_MIGRATIONS', raising=False)

    calls: List[str] = []

    def fake_engine(url: str) -> Any:
        calls.append(url)
        raise AssertionError('engine не должен создаваться без файлов миграций')

    monkeypatch.setattr(db_migrations, '_create_migration_engine', fake_engine)

    with caplog.at_level(logging.ERROR, logger='db_migrations'):
        result = db_migrations.apply_database_migrations()

    assert result is False
    assert calls == [], 'обращения к БД быть не должно'
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors and 'Файлы миграций не найдены' in errors[0].getMessage()


# === 5. Флаг RUN_MIGRATIONS: намеренный пропуск ===


@pytest.mark.parametrize('flag', ['false', 'FALSE', '0', 'no', 'No'])
def test_run_migrations_flag_skips_without_connection(flag, monkeypatch, caplog):
    """RUN_MIGRATIONS=false/0/no (любой регистр): True + INFO-лог, подключения нет.

    DATABASE_URL намеренно недоступен: если бы хелпер подключался, он вернул бы
    False — значит True доказывает пропуск до обращения к БД.
    """
    monkeypatch.setenv('RUN_MIGRATIONS', flag)
    monkeypatch.setenv('DATABASE_URL', UNREACHABLE_URL)

    calls: List[str] = []

    def forbidden_engine(url: str) -> Any:
        calls.append(url)
        raise AssertionError('намеренный пропуск не должен создавать engine')

    monkeypatch.setattr(db_migrations, '_create_migration_engine', forbidden_engine)

    with caplog.at_level(logging.INFO, logger='db_migrations'):
        result = db_migrations.apply_database_migrations()

    assert result is True
    assert calls == []
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert infos and 'пропущено' in infos[0].getMessage().lower()


def test_without_flag_migrations_are_applied(sqlite_url, monkeypatch):
    """Дефолт (переменной нет) — миграции выполняются; пустое значение — тоже."""
    monkeypatch.delenv('RUN_MIGRATIONS', raising=False)
    assert db_migrations.apply_database_migrations() is True
    assert 'alembic_version' in table_names(sqlite_url)

    monkeypatch.setenv('RUN_MIGRATIONS', '')
    assert db_migrations.apply_database_migrations() is True


def test_engine_disposed_after_run(sqlite_url, monkeypatch):
    """Engine одноразовый: dispose() вызывается на успешном пути (NullPool)."""
    from sqlalchemy.engine import Engine

    disposed: List[bool] = []
    real_dispose = Engine.dispose

    def spy_dispose(self: Any, close: bool = True) -> Any:
        disposed.append(True)
        return real_dispose(self, close)

    monkeypatch.setattr(Engine, 'dispose', spy_dispose)

    assert db_migrations.apply_database_migrations() is True
    assert disposed == [True], 'ожидался ровно один dispose() engine хелпера'


# === 6. Менеджеры БД: до миграций fail-silent, после — работают ===


def test_managers_work_after_migrations(sqlite_url):
    """Поддержка MODIFIED-требования T1: корень бага 2 закрыт миграциями."""
    app = make_app(sqlite_url)
    feedback = FeedbackManager(app=app)

    # До наката схемы таблиц нет — менеджеры fail-silent.
    assert feedback.set_reaction(USER_ID, MOVIE_ID, FeedbackManager.REACTION_WATCHED) is False
    assert StatsManager(app=app).get_user_stats(USER_ID)['available'] is False

    assert db_migrations.apply_database_migrations() is True

    # После наката — штатная работа: реакция пишется и читается, сводка доступна.
    assert feedback.set_reaction(USER_ID, MOVIE_ID, FeedbackManager.REACTION_NOPE) is True
    assert feedback.set_rating(USER_ID, MOVIE_ID, 8) is True
    stored = feedback.get_feedback(USER_ID, MOVIE_ID)
    assert stored is not None
    assert stored['reaction'] == FeedbackManager.REACTION_NOPE
    assert stored['rating'] == 8

    stats = StatsManager(app=app).get_user_stats(USER_ID)
    assert stats['available'] is True
    assert stats['nope'] == 1
    assert stats['rated'] == 1


def test_empty_database_url_falls_back_to_default(monkeypatch):
    """Пустая `DATABASE_URL=` трактуется как незаданная (fallback db_bootstrap).

    `os.getenv('DATABASE_URL') or DEFAULT_DATABASE_URL`: иначе пустая строка
    из .env.production дошла бы до `create_engine('')` (ArgumentError).
    """
    monkeypatch.setenv('DATABASE_URL', '')
    monkeypatch.delenv('RUN_MIGRATIONS', raising=False)

    captured: List[str] = []

    def spy_engine(url: str) -> Any:
        captured.append(url)
        raise RuntimeError('остановка теста до подключения к БД')

    monkeypatch.setattr(db_migrations, '_create_migration_engine', spy_engine)

    assert db_migrations.apply_database_migrations() is False
    assert captured == [db_bootstrap.DEFAULT_DATABASE_URL]


# === 7. Логирование процесса не переконфигурируется ===


def test_programmatic_run_keeps_existing_loggers(sqlite_url):
    """configure_logger=False: логгеры и handlers процесса бота сохраняются."""
    bot_logger = logging.getLogger('telegram_bot.t12_probe')
    marker = logging.StreamHandler()
    bot_logger.addHandler(marker)
    bot_logger.setLevel(logging.INFO)
    root_handlers_before = list(logging.getLogger().handlers)

    assert db_migrations.apply_database_migrations() is True

    assert bot_logger.disabled is False
    assert marker in bot_logger.handlers
    assert bot_logger.level == logging.INFO
    # fileConfig заменил бы handlers root-логгера (консоль вместо файла бота).
    assert root_handlers_before == list(logging.getLogger().handlers)

    bot_logger.removeHandler(marker)


def test_config_attributes_connection_and_configure_logger(sqlite_url, monkeypatch):
    """Хелпер передаёт свой engine через cfg.attributes и гасит fileConfig."""
    captured: Dict[str, Any] = {}

    def fake_upgrade(cfg: Any, revision: str, **kwargs: Any) -> None:
        captured['revision'] = revision
        captured['connection'] = cfg.attributes.get('connection')
        captured['configure_logger'] = cfg.attributes.get('configure_logger')
        captured['ini'] = cfg.config_file_name

    import alembic.command

    monkeypatch.setattr(alembic.command, 'upgrade', fake_upgrade)

    assert db_migrations.apply_database_migrations() is True
    assert captured['revision'] == 'head'
    assert captured['configure_logger'] is False
    assert captured['connection'] is not None
    # ini — из КОРНЯ проекта (путь не зависит от cwd).
    assert captured['ini'].endswith('alembic.ini')
    assert captured['ini'].startswith(ROOT)


def test_alembic_config_reads_ini_as_utf8_regardless_of_locale(monkeypatch):
    """ini читается ЯВНО в UTF-8 нашим подклассом Config (а не в кодировке локали).

    alembic использует `configparser.read(..., encoding='locale')`: без явного
    UTF-8 русские комментарии alembic.ini роняли бы чтение на Windows (cp1251),
    и миграции при старте бота молча не применялись. Проверяются: тип конфига
    (`Utf8AlembicConfig` — переопределённое свойство `file_config`, без инъекции
    во внутренний кэш alembic), фактическая кодировка чтения (spy на
    `ConfigParser.read`), кеширование парсера в экземпляре и разрешение
    `script_location` от пути ini (не зависит от cwd).
    """
    from configparser import ConfigParser

    encodings: List[Any] = []
    real_read = ConfigParser.read

    def spy_read(self: Any, filenames: Any, encoding: Any = None) -> Any:
        encodings.append(encoding)
        return real_read(self, filenames, encoding=encoding)

    monkeypatch.setattr(ConfigParser, 'read', spy_read)

    cfg = db_migrations._load_alembic_config(os.path.join(ROOT, 'alembic.ini'))
    assert isinstance(cfg, db_migrations.Utf8AlembicConfig)

    parser = cfg.file_config
    assert encodings == ['utf-8'], f'ожидалось чтение в UTF-8, получено: {encodings}'
    assert isinstance(parser, ConfigParser)
    # Кэш в экземпляре: повторное обращение не перечитывает файл.
    assert cfg.file_config is parser
    assert encodings == ['utf-8']

    script_location = cfg.get_main_option('script_location')
    assert '%(here)s' not in script_location
    assert script_location.replace('\\', '/').endswith('/migrations')
    assert os.path.isdir(script_location)
    # Русские комментарии ini прочитаны (значения секции доступны).
    assert cfg.get_main_option('output_encoding') == 'utf-8'


# === 8. Текстовые guard'ы артефактов деплоя (docker/CI не запускаются) ===


def test_dockerfile_delivers_migration_files():
    """Образ содержит migrations/ и alembic.ini (compose запускает image)."""
    dockerfile = read_text('Dockerfile')
    assert 'COPY migrations/ ./migrations/' in dockerfile
    assert 'COPY alembic.ini .' in dockerfile
    # UTF-8 для CLI alembic внутри контейнера (ini читается в кодировке локали).
    assert 'ENV PYTHONUTF8=1' in dockerfile
    # CMD и структура слоёв не менялись.
    assert 'CMD ["python", "src/telegram_bot.py"]' in dockerfile


def test_deploy_yml_backup_before_containers_and_migrations():
    """Порядок: бэкап < запуск контейнеров < миграции < админ < health-чек."""
    text = read_text('.github', 'workflows', 'deploy.yml')
    order = [
        'Резервное копирование БД (pg_dump)',
        'Запуск контейнеров',
        'Применение миграций БД (alembic upgrade head)',
        'Создание/проверка админа БД (init_db.py)',
        'Проверка результата деплоя',
    ]
    positions = [text.index(f'- name: {name}') for name in order]
    assert positions == sorted(positions), f'нарушен порядок шагов: {list(zip(order, positions))}'

    backup = deploy_step(text, order[0])
    backup_commands = step_commands(backup)
    # Дамп пишется ФАЙЛОМ .sql и затем сжимается `gzip -f` того же файла:
    # пайплайн `pg_dump ... | gzip` на удалённой стороне ЗАПРЕЩЁН — код возврата
    # пайплайна равен коду gzip, и сбой pg_dump оставил бы пустой архив при
    # «успешном» шаге (нарушение сценария «Сбой бэкапа прерывает деплой»).
    assert 'pg_dump' in backup_commands and '| gzip' not in backup_commands
    assert not re.search(r'pg_dump[^\n]*\|', backup_commands), 'пайплайн с pg_dump недопустим'
    assert 'mkdir -p ~/kinobot/backups' in backup_commands
    assert 'docker exec kinobot_postgres pg_dump -U postgres kinobot_db > \\$DUMP' in backup_commands
    assert 'gzip -f \\$DUMP' in backup_commands
    assert 'DUMP=~/kinobot/backups/kinobot_db_\\$(date +%F_%H%M).sql' in backup_commands
    # Ротация — 7 суток, цепочка на `&&` (сбой любого звена рвёт шаг).
    assert 'find ~/kinobot/backups' in backup_commands and '-mtime +7 -delete' in backup_commands
    assert backup_commands.count(' && \\') >= 3
    # «Контейнера нет» (первичный деплой) — предупреждение и пропуск;
    # ЛЮБОЙ иной сбой ssh/docker — фатален (транспортная ошибка не должна
    # выглядеть как «бэкап не нужен»).
    assert 'No such object' in backup_commands
    assert '::warning::' in backup_commands and '::error::' in backup_commands
    assert 'exit 0' in backup_commands and 'exit 1' in backup_commands
    # Сбой бэкапа фатален: `|| true` в командах шага отсутствует.
    assert '|| true' not in backup_commands

    migrations_step = deploy_step(text, order[2])
    assert 'docker exec kinobot alembic upgrade head' in migrations_step
    # Фатальный шаг: ошибка + exit 1; `|| true` допустим только для сбора логов.
    commands = step_commands(migrations_step)
    assert '::error::' in commands and 'exit 1' in commands
    tolerant = [ln for ln in commands.splitlines() if '|| true' in ln]
    assert all('docker logs' in ln for ln in tolerant)

    admin_step = deploy_step(text, order[3])
    assert 'docker exec kinobot python init_db.py' in admin_step
    # Нефатальный шаг: предупреждение, без exit 1 (поглощает T8).
    admin_commands = step_commands(admin_step)
    assert '::warning::' in admin_commands and 'exit 1' not in admin_commands


def test_web_process_does_not_run_migrations():
    """`src/app.py` не импортирует и не вызывает хелпер миграций (один раннер)."""
    app_source = read_text('src', 'app.py')
    assert 'db_migrations' not in app_source
    assert 'apply_database_migrations' not in app_source
    assert 'create_all' not in app_source


def test_compose_documents_single_runner():
    """compose: комментарий у сервиса kinobot — единственный раннер миграций."""
    compose = read_text('deploy', 'docker-compose.prod.yml')
    # Комментарий — блок строк непосредственно перед объявлением сервиса.
    before_service = compose.split('  kinobot:', 1)[0]
    comment_lines: List[str] = []
    for line in reversed(before_service.splitlines()):
        if line.strip().startswith('#'):
            comment_lines.insert(0, line)
        elif comment_lines:
            break
    comment = '\n'.join(comment_lines)
    assert 'ЕДИНСТВЕННЫЙ раннер миграций' in comment
    assert 'RUN_MIGRATIONS=false' in comment
    assert 'kinobot-web' in comment and 'apply_database_migrations' in comment
    # Env-флаг в compose намеренно не задан (дефолт — бот мигрирует).
    assert 'RUN_MIGRATIONS:' not in compose


def test_env_py_fileconfig_guard():
    """migrations/env.py: fileConfig только при configure_logger (дефолт True)."""
    env_source = read_text('migrations', 'env.py')
    assert "config.attributes.get('configure_logger', True)" in env_source
    # Точка интеграции programmatic-запуска сохранена.
    assert "config.attributes.get('connection', None)" in env_source


def test_telegram_bot_uses_migrations_helper():
    """Бот: импорт и вызов хелпера миграций вместо легаси-хука create_all."""
    bot_source = read_text('src', 'telegram_bot.py')
    assert 'from db_migrations import apply_database_migrations' in bot_source
    assert '    apply_database_migrations()' in bot_source
    # Легаси-хук T1 из бота больше не импортируется (модуль сохранён как fallback).
    assert 'from db_bootstrap import' not in bot_source


def test_dev_guide_has_t12_operator_instructions():
    """dev_guide: подраздел T12 с инструкциями оператору."""
    guide = read_text('docs', 'dev_guide.md')
    assert '### Автоприменение при старте и в деплое (T12)' in guide
    for fragment in (
        'RUN_MIGRATIONS=false',
        'alembic stamp head',
        'gunzip -c ~/kinobot/backups/',
        'downgrade',
        'обратно совместима на один релиз',
        'ЕДИНСТВЕННЫЙ раннер миграций — бот',
        'ENV PYTHONUTF8=1',
    ):
        assert fragment in guide, f'в dev_guide.md нет фрагмента: {fragment}'
