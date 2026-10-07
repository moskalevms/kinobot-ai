# -*- coding: utf-8 -*-
"""Тесты T13: проверка миграций без реального PostgreSQL + guard'ы схемы.

Изменение add-migration-guard-tests-t13 (бэклог T13,
backlog/backlog_2026-10-04_bugfix_telegram_ui.md, строки 148-156).

Проверяется:
1. «upgrade head на пустой БД»: программный `alembic.command.upgrade` на
   временный SQLite-файл (tmp_path) — существуют все 8 таблиц моделей плюс
   `alembic_version` (версия head '0001'); повторный upgrade — no-op без
   исключений, данные и состав таблиц не затрагиваются.
2. «На существующей БД с данными»: таблицы созданы `db.create_all()`
   (эмуляция прод-БД после init_db.py), в `watchlist`/`movie_feedback`
   вставлены строки, программный `command.stamp(cfg, 'head')` — версия
   проставлена, данные целы; последующий `upgrade head` строки не роняет
   (guard'ы baseline T11 диалект-нейтральны через `sa.inspect` — работают
   и на SQLite в тестах, и на PostgreSQL на проде).
3. Guard «модели == миграции» (замена ловушки AGENTS.md «синхронизируй оба
   места»): `alembic.autogenerate.compare_metadata` метаданных моделей
   против накатанной head-схемы — diff пустой. Негативный контроль —
   искусственный рассинхрон (КОПИЯ метаданных с лишней колонкой/таблицей,
   реальные модели и миграции не правятся) даёт непустой diff: guard ловит
   «добавили колонку в модель — не создали миграцию».
4. Guard политики сохранности (additive-first): AST-скан
   `migrations/versions/*.py` — вызовы `drop_table`/`drop_column` (и SQL
   «DROP TABLE/COLUMN» строковыми константами) ЗАПРЕЩЕНЫ в `upgrade()` и на
   уровне модуля; единственное допустимое место — `downgrade()` baseline-файла
   из явного allow-списка (расширение списка — осознанный акт ручного ревью).
   Негативный контроль — сканер на временном каталоге с искусственной
   «миграцией», содержащей разрушающий DDL в upgrade (реальные файлы не правятся).

Реальный PostgreSQL, docker и сеть НЕ нужны: файловая SQLite в tmp_path
(паттерн tests/test_db_migrations_t12.py) и сканирование файлов репозитория.
Прогон модуля — единицы секунд (бюджет T13: < 10 с).
"""
import ast
import os
import re
from pathlib import Path
from typing import Any, Callable, List, Set, Tuple

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from flask import Flask
from conftest import ROOT

import db_migrations
from models.database import MovieFeedback, Watchlist, db

# Все таблицы моделей src/models/database.py (единственный источник схемы) —
# baseline-ревизия 0001_initial создаёт их полностью. Константа сверяется с
# самими моделями отдельным тестом (design.md D6) — «протухнуть» не может.
MODEL_TABLES: Set[str] = {
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

# Версия head после baseline T11 (единственная ревизия '0001').
HEAD_VERSION = '0001'

# Разрушающие операции Alembic (политика additive-first, migrations/README.md).
DESTRUCTIVE_OPS: Tuple[str, ...] = ('drop_table', 'drop_column')

# SQL-форма разрушающего DDL (для строковых констант вида
# `op.execute('ALTER TABLE ... DROP COLUMN ...')`): пробел между словами,
# поэтому docstring'и/комментарии с «drop_table» (подчёркивание) не ловит.
DESTRUCTIVE_SQL_RE = re.compile(r'DROP\s+(TABLE|COLUMN)', re.IGNORECASE)

# Allow-список мест, где разрушающий DDL допустим (design.md D4): только
# downgrade() baseline-ревизии (откат «создания всех таблиц» — единственная
# легитимная разрушающая операция, и та с предупреждением о запрете на проде).
# Ключ — имя файла ревизии, значение — имя функции. Расширение списка —
# осознанный акт, эквивалент ручного ревью (политика сохранности данных).
_ALLOWED_DOWNGRADE_DROPS: Set[str] = {'0001_initial.py'}


@pytest.fixture()
def sqlite_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """URL файловой SQLite в tmp_path; DATABASE_URL подменён для окружения Alembic.

    RUN_MIGRATIONS принудительно снят (аварийный выключатель T12 не должен
    влиять на тесты миграций); load_dotenv в migrations/env.py работает с
    override=False — значение из monkeypatch приоритетнее файла .env.
    """
    url = f"sqlite:///{(tmp_path / 'kinobot_t13.sqlite3').as_posix()}"
    monkeypatch.setenv('DATABASE_URL', url)
    monkeypatch.delenv('RUN_MIGRATIONS', raising=False)
    return url


@pytest.fixture()
def alembic_cfg() -> Config:
    """Конфигурация Alembic из alembic.ini в КОРНЕ проекта (design.md D1).

    Фабрика T12 `_load_alembic_config` читает ini ЯВНО в UTF-8 (русские
    комментарии на Windows/cp1251 иначе роняли бы чтение); атрибут
    configure_logger=False — fileConfig из alembic.ini не переконфигурирует
    логирование pytest-процесса (guard в migrations/env.py, точка T12).
    """
    cfg = db_migrations._load_alembic_config(os.path.join(ROOT, 'alembic.ini'))
    cfg.attributes['configure_logger'] = False
    return cfg


def run_alembic_command(cfg: Config, url: str, alembic_command: Callable[..., Any], revision: str) -> None:
    """Выполнить команду Alembic, передав engine через attributes['connection'].

    Точка интеграции migrations/env.py (паттерн src/db_migrations.py, D1):
    engine создаёт тест (NullPool — соединения не переживают команду) и
    закрывает его сам; атрибут после вызова снимается, чтобы следующая
    команда не получила disposed-engine. DATABASE_URL дополнительно подменён
    фикстурой на тот же файл — даже без атрибута env.py не ушёл бы на postgres.
    """
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        cfg.attributes['connection'] = engine
        alembic_command(cfg, revision)
    finally:
        cfg.attributes.pop('connection', None)
        engine.dispose()


def make_app(database_url: str) -> Any:
    """Минимальное Flask-приложение менеджеров (паттерн test_db_migrations_t12).

    Используется для `db.create_all()` (эмуляция прод-БД после init_db.py) и
    ORM-строк данных; подключается к той же файловой БД, что и команды Alembic.
    """
    app = Flask(f'test_t13_{id(database_url)}')
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


def table_names(url: str) -> Set[str]:
    """Имена таблиц БД через отдельный engine (sa.inspect) — диалект-нейтрально."""
    engine = sa.create_engine(url)
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def schema_version(url: str) -> str:
    """Текущая версия схемы из служебной таблицы `alembic_version` ('' — если нет)."""
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            row = connection.execute(sa.text('SELECT version_num FROM alembic_version')).first()
        return str(row[0]) if row else ''
    finally:
        engine.dispose()


def compare_with_metadata(url: str, metadata: sa.MetaData) -> List[Any]:
    """Diff `compare_metadata` схемы БД после наката head с переданными метаданными.

    ОГРАНИЧЕНИЕ (design.md D2, риск T13 бэклога): используются дефолтные opts
    Alembic — `compare_type=False` и `compare_server_default=False`. Сравнение
    типов/значений по умолчанию на SQLite даёт ЛОЖНЫЕ diff'ы (например,
    `DateTime(timezone=True)` отражается как DATETIME, `func.now()` в моделях
    против `CURRENT_TIMESTAMP` в baseline), поэтому guard сравнивает структуру
    (таблицы/колонки/индексы/ограничения по именам и составу). Резервный план
    при будущих ложных срабатываниях — сузить сравнение до имён таблиц/колонок.
    Guard НЕ заменяет ручное ревью миграций.
    """
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            return list(compare_metadata(context, metadata))
    finally:
        engine.dispose()


def _find_destructive(node: ast.AST) -> List[Tuple[int, str]]:
    """Разрушающие операции в AST-поддереве: список (номер строки, описание).

    Ловятся две формы (design.md D4):
    - вызов-атрибут `drop_table`/`drop_column` (объект не важен — покрывает
      и `op.*`, и `batch_op.*` пакетного режима);
    - строковая константа с SQL-паттерном `DROP TABLE`/`DROP COLUMN`
      (регистронезависимо) — ручной SQL через `op.execute(...)`.
    Комментарии и docstring'и с формой «drop_table» (подчёркивание) не
    срабатывают: AST не содержит комментариев, а regex требует пробел.
    """
    found: List[Tuple[int, str]] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr in DESTRUCTIVE_OPS:
            found.append((sub.lineno, sub.func.attr))
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) and DESTRUCTIVE_SQL_RE.search(sub.value):
            found.append((sub.lineno, 'SQL DROP TABLE/COLUMN'))
    return found


def scan_destructive_ddl(versions_dir: str) -> List[str]:
    """AST-скан каталога ревизий: разрушающий DDL вне разрешённых мест (D4).

    Возвращает список нарушений «файл:строка — операция в области». Правило:
    `drop_table`/`drop_column` (вызовы и SQL-строки) ЗАПРЕЩЕНЫ в функциях
    `upgrade()`, во вспомогательных функциях и в коде уровня модуля; допустимы
    ТОЛЬКО в `downgrade()` файлов из `_ALLOWED_DOWNGRADE_DROPS` (baseline).
    Каталог параметризован — негативный контроль сканирует временный каталог,
    реальные `migrations/versions/` не правятся (design.md D5).
    """
    violations: List[str] = []
    for name in sorted(os.listdir(versions_dir)):
        if not name.endswith('.py'):
            continue
        path = os.path.join(versions_dir, name)
        with open(path, encoding='utf-8') as fh:
            tree = ast.parse(fh.read(), filename=path)

        downgrade_allowed = name in _ALLOWED_DOWNGRADE_DROPS
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == 'downgrade' and downgrade_allowed:
                    continue
                scope = f'в функции {node.name}()'
            else:
                scope = 'на уровне модуля'
            for line, op_name in _find_destructive(node):
                violations.append(f'{name}:{line} — {op_name} {scope}')
    return violations


# === 0. Сверка константы таблиц с моделями (страховка D6) ===


def test_model_tables_constant_matches_metadata() -> None:
    """MODEL_TABLES == таблицы метаданных моделей: константа теста не «протухла»."""
    assert MODEL_TABLES == set(db.metadata.tables)


# === 1. upgrade head на пустой БД (сценарий 1 спеки) ===


def test_upgrade_head_on_empty_db_creates_all_tables(sqlite_url: str, alembic_cfg: Config) -> None:
    """Пустая БД: upgrade head создаёт все 8 таблиц моделей + alembic_version."""
    run_alembic_command(alembic_cfg, sqlite_url, command.upgrade, 'head')

    tables = table_names(sqlite_url)
    assert MODEL_TABLES <= tables, f'не созданы таблицы моделей: {MODEL_TABLES - tables}'
    assert 'alembic_version' in tables
    assert schema_version(sqlite_url) == HEAD_VERSION


def test_repeated_upgrade_is_noop(sqlite_url: str, alembic_cfg: Config) -> None:
    """Повторный upgrade head — no-op: без исключений, данные и таблицы целы."""
    run_alembic_command(alembic_cfg, sqlite_url, command.upgrade, 'head')

    app = make_app(sqlite_url)
    with app.app_context():
        db.session.add(Watchlist(user_id=USER_ID, kinopoisk_id=MOVIE_ID, title='Начало'))
        db.session.commit()

    tables_before = table_names(sqlite_url)
    run_alembic_command(alembic_cfg, sqlite_url, command.upgrade, 'head')

    assert table_names(sqlite_url) == tables_before, 'повторный upgrade изменил состав таблиц'
    assert schema_version(sqlite_url) == HEAD_VERSION
    with app.app_context():
        stored = db.session.query(Watchlist).filter_by(user_id=USER_ID).one()
        assert stored.kinopoisk_id == MOVIE_ID
        assert stored.title == 'Начало'


# === 2. Существующая БД с данными: create_all + stamp head + upgrade head ===


def test_stamp_head_on_create_all_db_preserves_data(sqlite_url: str, alembic_cfg: Config) -> None:
    """Эмуляция прод-БД (init_db.py): stamp head проставляет версию, данные целы.

    Сценарий первичного внедрения из docs/dev_guide.md (T12): БД создана
    create_all и содержит данные → `alembic stamp head` (без DDL) → деплои
    накатывают `upgrade head` автоматически. Guard'ы baseline учитывают
    диалект: проверка существования таблиц через `sa.inspect` работает и на
    SQLite (тест), и на PostgreSQL (прод) — upgrade не пересоздаёт таблицы и
    не роняет строки.
    """
    app = make_app(sqlite_url)
    with app.app_context():
        db.create_all()
        db.session.add(Watchlist(user_id=USER_ID, kinopoisk_id=MOVIE_ID, title='Начало'))
        db.session.add(MovieFeedback(user_id=USER_ID, kinopoisk_id=MOVIE_ID, reaction='nope', rating=8))
        db.session.commit()

    # create_all не создаёт alembic_version — stamp накатывает версию впервые.
    assert 'alembic_version' not in table_names(sqlite_url)

    run_alembic_command(alembic_cfg, sqlite_url, command.stamp, 'head')
    assert schema_version(sqlite_url) == HEAD_VERSION, 'stamp head не проставил версию'

    run_alembic_command(alembic_cfg, sqlite_url, command.upgrade, 'head')

    assert schema_version(sqlite_url) == HEAD_VERSION
    assert MODEL_TABLES <= table_names(sqlite_url)
    with app.app_context():
        stored_wl = db.session.query(Watchlist).filter_by(user_id=USER_ID).one()
        assert stored_wl.title == 'Начало'
        stored_mf = db.session.query(MovieFeedback).filter_by(user_id=USER_ID).one()
        assert stored_mf.reaction == 'nope'
        assert stored_mf.rating == 8


# === 3. Guard «модели == миграции» (compare_metadata) ===


def test_models_match_migrations_head(sqlite_url: str, alembic_cfg: Config) -> None:
    """Diff метаданных моделей с накатанной head-схемой пуст: модели == миграции.

    Guard заменяет ловушку AGENTS.md «модели БД продублированы —
    синхронизируй оба места» для пары «модели ↔ миграции»: колонка/таблица,
    добавленная в src/models/database.py без новой ревизии, роняет этот тест.
    Ограничение сравнения (типы/server_default не сравниваются — дефолт
    Alembic против ложных diff'ов на SQLite) — см. compare_with_metadata.
    """
    run_alembic_command(alembic_cfg, sqlite_url, command.upgrade, 'head')

    diff = compare_with_metadata(sqlite_url, db.metadata)
    assert diff == [], f'рассинхрон моделей и миграций (нужна новая ревизия): {diff}'


def test_guard_detects_artificial_desync(sqlite_url: str, alembic_cfg: Config) -> None:
    """Негативный контроль: искусственный рассинхрон guard ЛОВИТ (diff непустой).

    КОПИЯ метаданных (`Table.to_metadata`) с дополнительной колонкой/таблицей —
    реальные модели и миграции НЕ правятся (design.md D3): копия — отдельный
    объект MetaData, глобальный db.metadata не затрагивается.
    """
    run_alembic_command(alembic_cfg, sqlite_url, command.upgrade, 'head')

    # Контроль 1: «добавили колонку в модель — не создали миграцию».
    probe_columns = sa.MetaData()
    for table in db.metadata.tables.values():
        table.to_metadata(probe_columns)
    probe_columns.tables['watchlist'].append_column(sa.Column('t13_probe', sa.String(10)))

    diff = compare_with_metadata(sqlite_url, probe_columns)
    assert diff, 'guard не заметил лишнюю колонку в метаданных (рассинхрон не ловится)'
    add_columns = [d for d in diff if d and d[0] == 'add_column']
    assert any(
        d[2] == 'watchlist' and getattr(d[3], 'name', None) == 't13_probe'
        for d in add_columns
    ), f'среди diff нет add_column для t13_probe: {diff}'

    # Контроль 2: «добавили таблицу в модель — не создали миграцию».
    probe_tables = sa.MetaData()
    for table in db.metadata.tables.values():
        table.to_metadata(probe_tables)
    sa.Table('t13_probe_table', probe_tables, sa.Column('id', sa.Integer(), primary_key=True))

    diff_tables = compare_with_metadata(sqlite_url, probe_tables)
    assert diff_tables, 'guard не заметил лишнюю таблицу в метаданных'
    assert any(
        d and d[0] == 'add_table' and getattr(d[1], 'name', None) == 't13_probe_table'
        for d in diff_tables
    ), f'среди diff нет add_table для t13_probe_table: {diff_tables}'


# === 4. Guard политики сохранности: запрет разрушающего DDL в upgrade-пути ===


def test_no_destructive_ddl_in_upgrade_path() -> None:
    """Реальные migrations/versions/: drop_table/drop_column только в downgrade baseline."""
    versions_dir = os.path.join(ROOT, 'migrations', 'versions')
    violations = scan_destructive_ddl(versions_dir)
    assert violations == [], f'разрушающий DDL в upgrade-пути: {violations}'

    # Контроль «сканер не холостой»: без allow-списка он обязан найти
    # drop_table в downgrade() реального baseline (8 таблиц).
    real_allowlist = _ALLOWED_DOWNGRADE_DROPS.copy()
    try:
        _ALLOWED_DOWNGRADE_DROPS.clear()
        assert scan_destructive_ddl(versions_dir), 'сканер не находит даже явный drop_table — guard холостой'
    finally:
        _ALLOWED_DOWNGRADE_DROPS.update(real_allowlist)


def test_guard_detects_artificial_drop_table(tmp_path: Path) -> None:
    """Негативный контроль: искусственная «миграция» с drop_table в upgrade — ловится.

    Временный каталог (tmp_path) с файлом ревизии, содержащим все запрещённые
    формы: `op.drop_table` и `batch_op.drop_column` в upgrade(), SQL
    «DROP COLUMN» через op.execute, `op.drop_table` на уровне модуля. Реальные
    файлы migrations/versions/ НЕ правятся (design.md D5). downgrade() того же
    файла тоже в нарушении — файл не входит в allow-список baseline.
    """
    bad_source = '''"""Искусственная ревизия для негативного контроля guard'а T13."""
from alembic import op

revision = '9999'
down_revision = '0001'

op.drop_table('rt_scores')


def upgrade() -> None:
    op.drop_table('watchlist')
    with op.batch_alter_table('movie_feedback') as batch_op:
        batch_op.drop_column('rating')
    op.execute('ALTER TABLE users DROP COLUMN email')


def downgrade() -> None:
    op.drop_table('offtopic_refusals')
'''
    bad_dir = tmp_path / 'versions_bad'
    bad_dir.mkdir()
    (bad_dir / '9999_bad.py').write_text(bad_source, encoding='utf-8')

    violations = scan_destructive_ddl(str(bad_dir))
    joined = '\n'.join(violations)
    # 5 нарушений: модульный уровень, 3 в upgrade(), 1 в downgrade() (файл
    # вне allow-списка). Меньше — значит сканер пропускает запрещённые формы.
    assert len(violations) == 5, f'ожидалось 5 нарушений, получено: {violations}'
    assert 'уровне модуля' in joined
    assert joined.count('функции upgrade()') == 3
    assert 'drop_table' in joined and 'drop_column' in joined
    assert 'SQL DROP TABLE/COLUMN' in joined
    assert 'функции downgrade()' in joined, 'downgrade вне allow-списка обязан быть нарушением'

    # Allow-список работает по имени файла: тот же разрушающий downgrade в
    # файле '0001_initial.py' нарушением НЕ считается (upgrade — по-прежнему да).
    allowed_dir = tmp_path / 'versions_allowed'
    allowed_dir.mkdir()
    (allowed_dir / '0001_initial.py').write_text(bad_source, encoding='utf-8')

    allowed_violations = scan_destructive_ddl(str(allowed_dir))
    assert 'downgrade()' not in '\n'.join(allowed_violations), 'allow-список не разрешил downgrade baseline'
    assert len(allowed_violations) == 4, f'upgrade/модульный уровень обязаны остаться нарушениями: {allowed_violations}'
