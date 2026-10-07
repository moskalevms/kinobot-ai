# -*- coding: utf-8 -*-
"""Тесты T14: init_db.py — тонкая обёртка (миграции схемы + админ).

Изменение update-init-db-thin-wrapper-t14: `init_db.py` больше не создаёт
схему `db.create_all()` — он накатывает `alembic upgrade head` хелпером
`db_migrations.apply_database_migrations()` (единственный источник DDL —
`migrations/`) и при сбое миграций fail-loud завершается с кодом 1 (семантика
CLI-инициализатора отличается от fail-silent старта бота), после чего
идемпотентно создаёт роль/пользователя 'admin'.

Проверяется РЕАЛЬНЫЙ CLI-контракт прогоном `python init_db.py` субпроцессом
из корня репозитория:
- чистая БД → exit 0: все 8 таблиц моделей + `alembic_version` с версией head,
  роль 'admin' и пользователь 'admin' созданы;
- идемпотентность: повторный прогон сохраняет строку watchlist, вставленную
  между запусками, админ остаётся один, exit 0;
- аварийный выключатель `RUN_MIGRATIONS=false`: exit 0, нейтральное сообщение
  о ПРОПУСКЕ миграций со значением флага (не «миграции применены» — схема не
  накатывалась), админ создан, схема и данные предварительного прогона целы;
- провал миграций (недоступный postgres 127.0.0.1:1 — немедленный connection
  refused, connect_timeout хелпера страхует «молчащий» хост) → exit 1
  (fail-loud), создание админа не выполняется.

Реальный PostgreSQL, docker и Telegram НЕ нужны: файловая SQLite в tmp_path
(образец tests/test_db_migrations_t12.py — оттуда переиспользуются хелперы
MODEL_TABLES/table_names/alembic_version/UNREACHABLE_URL). Прогон — единицы-
десятки секунд (шесть запусков субпроцесса).
"""
import os
import subprocess
import sys
from typing import Any, Tuple

import pytest
from flask import Flask

from conftest import ROOT
from models.database import Role, User, Watchlist, db
from test_db_migrations_t12 import (
    MODEL_TABLES,
    UNREACHABLE_URL,
    alembic_version,
    table_names,
)

ADMIN_PASSWORD = 'test-password-t14'

# Запас на старт субпроцесса и импорт flask/sqlalchemy/alembic (штатно —
# единицы секунд); превышение — явный TimeoutExpired, а не вечный вис.
INIT_DB_TIMEOUT_SECONDS = 180


@pytest.fixture()
def sqlite_url(tmp_path) -> str:
    """URL файловой SQLite в tmp_path (файл БД создаёт сам init_db.py)."""
    return f"sqlite:///{(tmp_path / 'kinobot_t14.sqlite3').as_posix()}"


def head_version() -> str:
    """Head цепочки ревизий через ScriptDirectory (ini читается в UTF-8 — хелпер T12)."""
    from alembic.script import ScriptDirectory

    import db_migrations

    cfg = db_migrations._load_alembic_config(os.path.join(ROOT, 'alembic.ini'))
    return str(ScriptDirectory.from_config(cfg).get_current_head())


def run_init_db(database_url: str, run_migrations: str = '') -> 'subprocess.CompletedProcess[str]':
    """Прогон `python init_db.py` из корня репо субпроцессом с явным env.

    Критичные переменные задаются ЯВНО: `load_dotenv()` внутри init_db.py не
    переопределяет уже установленные значения (защита от локального `.env`
    разработчика). `PYTHONIOENCODING=utf-8` — пайп stdout на Windows по
    умолчанию cp1251, без него emoji-print'ы падают UnicodeEncodeError;
    `RUN_MIGRATIONS` — по умолчанию пустая строка: она не входит во флаги
    отключения (false/0/no) и гарантирует штатный накат миграций; значение
    `'false'` воспроизводит аварийный выключатель оператора (накат пропущен).
    Превышение `timeout` — явный `subprocess.TimeoutExpired`, а не вечный вис.
    """
    env = os.environ.copy()
    env.update({
        'DATABASE_URL': database_url,
        'ADMIN_PASSWORD': ADMIN_PASSWORD,
        'PYTHONIOENCODING': 'utf-8',
        'RUN_MIGRATIONS': run_migrations,
    })
    return subprocess.run(
        [sys.executable, 'init_db.py'],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=INIT_DB_TIMEOUT_SECONDS,
    )


def make_app(database_url: str) -> Any:
    """Минимальное Flask-приложение к той же БД (паттерн tests/test_db_migrations_t12)."""
    app = Flask(f'test_t14_{id(database_url)}')
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


def insert_watchlist_row(database_url: str) -> None:
    """Строка watchlist между прогонами (проверка сохранности данных); соединения закрываются."""
    app = make_app(database_url)
    with app.app_context():
        db.session.add(Watchlist(user_id='u1', kinopoisk_id=447301, title='Дюна'))
        db.session.commit()
        db.session.remove()
        db.engine.dispose()


def watchlist_and_admin_counts(database_url: str) -> Tuple[int, int, int]:
    """(строки watchlist, юзеры 'admin', роли 'admin') — состояние после прогонов."""
    app = make_app(database_url)
    with app.app_context():
        rows = db.session.query(Watchlist).count()
        admins = db.session.query(User).filter_by(username='admin').count()
        admin_roles = db.session.query(Role).filter_by(name='admin').count()
        db.session.remove()
        db.engine.dispose()
    return rows, admins, admin_roles


def test_clean_db_schema_and_admin_created(sqlite_url: str) -> None:
    """Первый прогон на чистой БД: exit 0, 8 таблиц + alembic_version=head, админ создан."""
    result = run_init_db(sqlite_url)
    assert result.returncode == 0, f'init_db.py упал:\n{result.stdout}\n{result.stderr}'

    tables = table_names(sqlite_url)
    assert MODEL_TABLES <= tables
    assert 'alembic_version' in tables
    # Схема сразу в актуальной версии — ручной `alembic stamp head` не нужен.
    assert alembic_version(sqlite_url) == head_version()

    rows, admins, admin_roles = watchlist_and_admin_counts(sqlite_url)
    assert rows == 0
    assert admins == 1
    assert admin_roles == 1
    assert 'База данных полностью инициализирована' in result.stdout


def test_rerun_is_idempotent_and_preserves_data(sqlite_url: str) -> None:
    """Повторный прогон: exit 0, строка watchlist между запусками цела, админ один."""
    first = run_init_db(sqlite_url)
    assert first.returncode == 0, f'первый прогон упал:\n{first.stdout}\n{first.stderr}'
    insert_watchlist_row(sqlite_url)

    second = run_init_db(sqlite_url)
    assert second.returncode == 0, f'повторный прогон упал:\n{second.stdout}\n{second.stderr}'
    # Обе ветки админа идемпотентны — повторный прогон ничего не пересоздаёт.
    assert 'уже существует' in second.stdout

    rows, admins, admin_roles = watchlist_and_admin_counts(sqlite_url)
    assert (rows, admins, admin_roles) == (1, 1, 1)
    assert alembic_version(sqlite_url) == head_version()


def test_run_migrations_false_reports_skip_and_still_creates_admin(sqlite_url: str) -> None:
    """RUN_MIGRATIONS=false: exit 0, сообщение о ПРОПУСКЕ (не об успехе), админ, данные целы.

    Интеграция kill-switch'а хелпера (`db_migrations.migrations_disabled()`)
    с CLI-выводом: хелпер возвращает True и при намеренном пропуске, поэтому
    скрипт обязан печатать НЕЙТРАЛЬНОЕ сообщение со значением флага — иначе
    оператор решит, что схема накатана, хотя она не изменялась. Схема
    подготавливается предварительным штатным прогоном (без флага), между
    прогонами вставляется строка watchlist: при пропуске подключения к БД нет,
    схема не пересоздаётся и данные сохраняются, а идемпотентная логика
    роли/юзера 'admin' работает независимо от флага.
    """
    prepared = run_init_db(sqlite_url)
    assert prepared.returncode == 0, f'подготовка схемы упала:\n{prepared.stdout}\n{prepared.stderr}'
    # Штатный путь печатает успех — ниже проверяем, что при пропуске его нет.
    assert 'миграции применены' in prepared.stdout
    insert_watchlist_row(sqlite_url)

    skipped = run_init_db(sqlite_url, run_migrations='false')
    assert skipped.returncode == 0, f'прогон с RUN_MIGRATIONS=false упал:\n{skipped.stdout}\n{skipped.stderr}'
    # Стабильные подстроки сообщения о пропуске (init_db.py) + значение флага.
    assert 'Миграции пропущены' in skipped.stdout
    assert 'RUN_MIGRATIONS=false' in skipped.stdout
    assert 'схема БД НЕ изменялась' in skipped.stdout
    assert 'миграции применены' not in skipped.stdout
    # Создание админа выполняется и при пропуске миграций (поток не сломан).
    assert 'уже существует' in skipped.stdout
    assert 'База данных полностью инициализирована' in skipped.stdout

    # Схема не пересоздавалась: строка watchlist цела, версия ревизии та же,
    # админ и роль — в единственном экземпляре.
    rows, admins, admin_roles = watchlist_and_admin_counts(sqlite_url)
    assert (rows, admins, admin_roles) == (1, 1, 1)
    assert alembic_version(sqlite_url) == head_version()


def test_unreachable_db_exits_1_fail_loud() -> None:
    """Провал миграций (недоступный postgres) → exit 1: CLI-семантика fail-loud."""
    # Connection refused на 127.0.0.1:1 немедленный; для «молчащего» хоста
    # ожидание ограничивает connect_timeout хелпера (db_bootstrap).
    result = run_init_db(UNREACHABLE_URL)
    assert result.returncode == 1, f'ожидался exit 1, получен {result.returncode}:\n{result.stdout}\n{result.stderr}'
    assert 'Не удалось применить миграции БД' in result.stdout
    # Fail-loud до создания админа: сообщение об инициализации не печатается.
    assert 'База данных полностью инициализирована' not in result.stdout
