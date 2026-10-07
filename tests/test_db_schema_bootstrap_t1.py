# -*- coding: utf-8 -*-
"""Тесты T1: идемпотентное создание схемы БД при старте бота.

Изменение add-db-schema-bootstrap-t1 (баг 2 бэклога
backlog_2026-10-04_bugfix_telegram_ui.md): на чистой БД без таблиц
`movie_feedback`/`watchlist`/`user_statistics` кнопки фидбека и
«Статистика» молча не работают — `FeedbackManager.set_reaction`
fail-silent возвращает False, `StatsManager.get_user_stats` отдаёт
`available=False`. Стартап-хук `db_bootstrap.ensure_database_schema()`
(`db.create_all()` через минимальное Flask-приложение) устраняет корень
бага: таблицы создаются при старте бота без ручного `python init_db.py`.

Проверяется:
- воспроизведение бага: до хука на чистой БД set_reaction → False,
  get_user_stats → available=False;
- хук создаёт таблицы movie_feedback, watchlist, user_statistics,
  offtopic_refusals, rt_scores (sqlalchemy.inspect) и возвращает True;
- идемпотентность: повторный вызов не падает, данные сохранены;
- после хука реакции/оценки сохраняются и читаются, сводка статистики
  available=True с учтёнными счётчиками (fb:watched/fb:nope/fb:score и
  menu:stats — на уровне менеджеров, без полного PTB-dispatch);
- fail-silent: недоступная БД (невалидный postgresql-URL) → False без
  исключения + ERROR-лог;
- точка вызова: main() вызывает стартап-хук ДО сборки приложения (порядок
  «hook → polling»); с T12 в этой точке вызывается
  `db_migrations.apply_database_migrations()` (`alembic upgrade head`), а
  `ensure_database_schema()` сохранён как legacy/fallback и проверяется здесь
  прямыми вызовами (изменение add-migrations-startup-deploy-t12).

Реальная БД/Telegram НЕ нужны: файловая SQLite в tmp_path (единая для
приложения хука и приложений менеджеров — in-memory sqlite:// не
разделяется между соединениями, design.md D6) и моки.
"""
import logging
from typing import Any, List

import pytest
from flask import Flask
from sqlalchemy import inspect

import db_bootstrap
import telegram_bot
from feedback_manager import FeedbackManager
from models.database import db
from stats_manager import StatsManager

# Таблицы бага 2 + кэш RT-оценок: всё, что создаёт create_all из моделей
# src/models/database.py (единственный источник схемы).
EXPECTED_TABLES = {
    'movie_feedback',
    'watchlist',
    'user_statistics',
    'offtopic_refusals',
    'rt_scores',
}

USER_ID = '777'
MOVIE_ID = 447301


@pytest.fixture()
def sqlite_url(tmp_path, monkeypatch):
    """URL файловой SQLite в tmp_path; DATABASE_URL подменён для хука."""
    url = f"sqlite:///{(tmp_path / 'kinobot_t1.sqlite3').as_posix()}"
    monkeypatch.setenv('DATABASE_URL', url)
    return url


def make_app(database_url: str) -> Any:
    """Минимальное приложение менеджера (паттерн _create_minimal_app).

    Таблицы НЕ создаёт — приложение подключается к той же файловой БД,
    которую (или не которую) подготовил хук.
    """
    app = Flask(f'test_t1_{id(database_url)}')
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


def table_names(app: Any) -> set:
    """Имена таблиц БД приложения (sqlalchemy.inspect)."""
    with app.app_context():
        return set(inspect(db.engine).get_table_names())


# === 1. Воспроизведение бага 2: чистая БД БЕЗ таблиц (критерий задачи) ===


def test_bug2_without_schema_feedback_and_stats_fail_silently(sqlite_url):
    """До хука (таблиц нет): set_reaction → False, get_user_stats → available=False.

    Фиксация корня бага 2: fail-silent менеджеров на чистой БД — именно это
    пользователь видел как «неработающие» кнопки на проде.
    """
    app = make_app(sqlite_url)
    feedback = FeedbackManager(app=app)
    assert feedback.set_reaction(USER_ID, MOVIE_ID, FeedbackManager.REACTION_WATCHED) is False
    assert feedback.set_rating(USER_ID, MOVIE_ID, 8) is False
    stats = StatsManager(app=app).get_user_stats(USER_ID)
    assert stats['available'] is False


# === 2. Хук создаёт схему (3.1) и идемпотентен (3.2) ===


def test_hook_creates_all_expected_tables_on_clean_db(sqlite_url):
    """Хук на чистой БД создаёт все таблицы бага 2 и возвращает True."""
    assert db_bootstrap.ensure_database_schema() is True
    assert EXPECTED_TABLES <= table_names(make_app(sqlite_url))


def test_hook_is_idempotent_and_preserves_data(sqlite_url):
    """Повторный вызов хука не падает (create_all checkfirst), данные целы."""
    assert db_bootstrap.ensure_database_schema() is True
    app = make_app(sqlite_url)
    feedback = FeedbackManager(app=app)
    assert feedback.set_reaction(USER_ID, MOVIE_ID, FeedbackManager.REACTION_NOPE) is True

    # Второй вызов на БД с таблицами и данными — без исключения, True.
    assert db_bootstrap.ensure_database_schema() is True

    stored = feedback.get_feedback(USER_ID, MOVIE_ID)
    assert stored is not None
    assert stored['reaction'] == FeedbackManager.REACTION_NOPE


# === 3. После хука операции работают (3.3) ===


def test_after_hook_feedback_saved_and_stats_available(sqlite_url):
    """fb:watched/fb:nope/fb:score/menu:stats на уровне менеджеров после хука.

    Полный PTB-dispatch не гоняется (критерий задачи допускает менеджеры
    напрямую): set_reaction/set_rating/get_feedback — путь callback'ов
    фидбека, get_user_stats — путь menu:stats.
    """
    assert db_bootstrap.ensure_database_schema() is True
    app = make_app(sqlite_url)

    feedback = FeedbackManager(app=app)
    # fb:watched → смена на fb:nope (upsert одной строки, B5).
    assert feedback.set_reaction(USER_ID, MOVIE_ID, FeedbackManager.REACTION_WATCHED) is True
    assert feedback.set_reaction(USER_ID, MOVIE_ID, FeedbackManager.REACTION_NOPE) is True
    # fb:score:{id}:8 — оценка панели 1–10.
    assert feedback.set_rating(USER_ID, MOVIE_ID, 8) is True
    stored = feedback.get_feedback(USER_ID, MOVIE_ID)
    assert stored is not None
    assert stored['reaction'] == FeedbackManager.REACTION_NOPE
    assert stored['rating'] == 8

    # menu:stats — сводка доступна и учитывает записанный фидбек.
    stats = StatsManager(app=app).get_user_stats(USER_ID)
    assert stats['available'] is True
    assert stats['nope'] == 1
    assert stats['watched'] == 0
    assert stats['rated'] == 1
    assert stats['avg_rating'] == 8.0


# === 4. Fail-silent при недоступной БД (3.4) ===


def test_hook_fail_silent_on_unreachable_db(monkeypatch, caplog):
    """Невалидный postgresql-URL: False без исключения, ERROR в логе."""
    # Порт 1 на localhost — connection refused немедленно (не блокирует прогон);
    # connect_timeout хука ограничивает ожидание и для «молчащего» хоста.
    monkeypatch.setenv('DATABASE_URL', 'postgresql://u:p@127.0.0.1:1/kinobot_db')
    with caplog.at_level(logging.ERROR, logger='db_bootstrap'):
        result = db_bootstrap.ensure_database_schema()
    assert result is False
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, 'ожидался ERROR-лог о сбое создания схемы'
    assert 'Не удалось создать схему БД' in errors[0].getMessage()


# === 5. Точка вызова: main() → хук ДО сборки приложения (3.5; с T12 — миграции) ===


def test_main_calls_hook_before_app_build(monkeypatch):
    """main() вызывает хелпер миграций (T12) до create_telegram_app/run_polling.

    До T12 в этой точке вызывался `ensure_database_schema()` (create_all);
    замена выполнена в той же точке стартапа — порядок «hook → polling»
    сохраняется (design.md D5 изменения add-migrations-startup-deploy-t12).
    """
    order: List[str] = []

    class FakeApplication:
        def run_polling(self, **kwargs: Any) -> None:
            order.append('polling')

    def fake_hook() -> bool:
        order.append('hook')
        return True

    monkeypatch.setattr(telegram_bot, 'apply_database_migrations', fake_hook)
    monkeypatch.setattr(telegram_bot, 'create_telegram_app', lambda: FakeApplication())
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token')

    telegram_bot.main()

    assert order == ['hook', 'polling']
