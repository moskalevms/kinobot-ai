# tests/test_rt_cache.py
"""Тесты B4: кэш RT-оценок в PostgreSQL.

Без сети и без живого PostgreSQL: in-memory SQLite с StaticPool и
check_same_thread=False (одна БД на все соединения и потоки — нужно для
async-обёрток через asyncio.to_thread). Fail-silent проверяется на
приложении с заведомо недоступным PostgreSQL (порт 1, connection
refused) — само подключение при этом не выполняется до первого запроса.
Флаг Epic B задаётся через monkeypatch.setenv — rt_scores_enabled читает
env в момент вызова (свойство B1). Продакшен-ветка работы со временем
(PostgreSQL TIMESTAMPTZ, aware-значения) покрывается без живой БД:
нормализацией _as_utc и компиляцией DDL под диалект postgresql.
conftest.py уже добавляет src/ в sys.path.
"""
import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask
from sqlalchemy import DateTime, SmallInteger, Text, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

import rt_cache
from models.database import RtScore, db
from omdb_client import RtScores


@pytest.fixture(autouse=True)
def flag_on(monkeypatch):
    """Включить feature flag Epic B и сбросить TTL на дефолт."""
    monkeypatch.setenv('ENABLE_RT_SCORES', 'true')
    monkeypatch.setenv('OMDB_API_KEY', 'test-omdb-key')
    monkeypatch.delenv('RT_CACHE_TTL_DAYS', raising=False)


@pytest.fixture(autouse=True)
def reset_module_state(monkeypatch):
    """Сброс глобального состояния модуля кэша между тестами.

    _app сбрасывается в None: иначе тест, забывший передать app=, молча ушёл бы
    в настоящий PostgreSQL (DATABASE_URL или дефолт localhost:5432) и зафиксировал
    глобальный движок на всю сессию pytest. _db_warned сбрасывается, чтобы
    однократное предупреждение о сбое БД не «наследовалось» соседними тестами.
    monkeypatch вернёт исходные значения при teardown.
    """
    monkeypatch.setattr(rt_cache, '_app', None)
    monkeypatch.setattr(rt_cache, '_db_warned', False)


def _dispose_app(test_app: Flask) -> None:
    """Освободить ресурсы приложения: сессию и пулы соединений (ревью n6)."""
    with test_app.app_context():
        db.session.remove()
        for engine in db.engines.values():
            engine.dispose()


@pytest.fixture()
def app():
    """Тестовое приложение с in-memory SQLite и созданными таблицами."""
    test_app = Flask('test_rt_cache')
    test_app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    test_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    # StaticPool + check_same_thread=False: одна in-memory БД для всех
    # соединений/потоков (без этого to_thread увидел бы пустую базу)
    test_app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'poolclass': StaticPool,
        'connect_args': {'check_same_thread': False},
    }
    db.init_app(test_app)
    with test_app.app_context():
        db.create_all()
        yield test_app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def broken_app():
    """Приложение с недоступным PostgreSQL: connection refused на порт 1."""
    test_app = Flask('test_rt_cache_broken')
    test_app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://kinobot:kinobot@127.0.0.1:1/kinobot_db'
    test_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(test_app)
    yield test_app
    # Teardown: без него каждый тест оставлял бы собственный движок/пул
    _dispose_app(test_app)


def _age_row(app, imdb_id: str, days: int) -> None:
    """Состарить fetched_at строки на days дней (моделирует протухание)."""
    with app.app_context():
        row = db.session.get(RtScore, imdb_id)
        assert row is not None
        row.fetched_at = datetime.now(timezone.utc) - timedelta(days=days)
        db.session.commit()


def _row_count(app) -> int:
    with app.app_context():
        return db.session.query(RtScore).count()


# --- Схема таблицы (критерий приёмки: create_all создаёт rt_scores) ---


def test_create_all_creates_rt_scores_table(app):
    """db.create_all() создаёт таблицу rt_scores с нужными колонками."""
    with app.app_context():
        inspector = inspect(db.engine)
        assert 'rt_scores' in inspector.get_table_names()
        columns = {c['name']: c for c in inspector.get_columns('rt_scores')}
        assert set(columns) == {'imdb_id', 'rt_score', 'metascore', 'fetched_at'}
        assert inspector.get_pk_constraint('rt_scores')['constrained_columns'] == ['imdb_id']
        assert columns['rt_score']['nullable'] is True
        assert columns['metascore']['nullable'] is True
        assert columns['fetched_at']['nullable'] is False


def test_model_column_types():
    """Типы колонок модели: TEXT PK, SMALLINT NULL, DateTime(timezone=True)."""
    table = RtScore.__table__
    assert isinstance(table.columns['imdb_id'].type, Text)
    assert table.columns['imdb_id'].primary_key is True
    assert isinstance(table.columns['rt_score'].type, SmallInteger)
    assert isinstance(table.columns['metascore'].type, SmallInteger)
    fetched_at_type = table.columns['fetched_at'].type
    assert isinstance(fetched_at_type, DateTime)
    # timezone=True: TIMESTAMPTZ в PostgreSQL, DATETIME в SQLite
    assert fetched_at_type.timezone is True


def test_ddl_is_timestamptz_in_postgresql():
    """Продакшен-ветка схемы: DDL под диалектом PostgreSQL даёт TIMESTAMPTZ.

    Требование спеки «в PostgreSQL колонка имеет TIMESTAMP WITH TIME ZONE»
    проверяется компиляцией DDL — живая база не нужна.
    """
    ddl = str(CreateTable(RtScore.__table__).compile(dialect=postgresql.dialect()))
    assert 'TIMESTAMP WITH TIME ZONE' in ddl


def test_fetched_at_default_is_set(app):
    """fetched_at проставляется default'ом, если не задан явно."""
    with app.app_context():
        db.session.add(RtScore(imdb_id='tt0000001', rt_score=10, metascore=None))
        db.session.commit()
        row = db.session.get(RtScore, 'tt0000001')
        assert row.fetched_at is not None


# --- Контракт результата чтения: miss / hit / negative hit / is_negative ---


def test_get_returns_miss_when_no_row(app):
    """Нет записи — miss: вызывающий код может идти в OMDb."""
    lookup = rt_cache.get_scores('tt0111161', app=app)
    assert lookup == rt_cache.MISS
    assert lookup.hit is False
    assert lookup.scores is None
    assert lookup.is_negative is False


def test_get_returns_hit_after_set(app):
    """Актуальная запись с оценками — hit (и не отрицательный кэш)."""
    rt_cache.set_scores('tt0111161', RtScores(91, 82), app=app)
    lookup = rt_cache.get_scores('tt0111161', app=app)
    assert lookup.hit is True
    assert lookup.scores == RtScores(rt_score=91, metascore=82)
    assert lookup.is_negative is False


def test_negative_hit_prevents_refetch(app):
    """Обе оценки NULL — negative hit: hit=True, в OMDb повторно НЕ идём.

    Контракт потребителя (B5): if not lookup.hit -> обращаться к OMDb;
    if lookup.is_negative -> оценок нет, бейджи не показываем.
    Здесь hit=True, значит повторный запрос к OMDb запрещён, а
    scores=RtScores(None, None) означает «оценок нет».
    """
    rt_cache.set_scores('tt9999999', RtScores(None, None), app=app)
    lookup = rt_cache.get_scores('tt9999999', app=app)
    assert lookup.hit is True
    assert lookup.scores == RtScores(rt_score=None, metascore=None)
    assert lookup.is_negative is True


def test_partial_scores_are_hit_not_negative(app):
    """Только одна оценка непустая — это hit, а не negative hit."""
    rt_cache.set_scores('tt0111161', RtScores(75, None), app=app)
    lookup = rt_cache.get_scores('tt0111161', app=app)
    assert lookup.hit is True
    assert lookup.scores == RtScores(rt_score=75, metascore=None)
    assert lookup.is_negative is False


def test_cache_lookup_rejects_hit_without_scores():
    """Инвариант CacheLookup защищён типом: hit=True без scores недопустим.

    Без проверки потребитель (B5) получил бы AttributeError
    «'NoneType' object has no attribute 'rt_score'» прямо в выдаче.
    """
    with pytest.raises(ValueError, match='hit=True требует непустой scores'):
        rt_cache.CacheLookup(hit=True, scores=None)


def test_cache_lookup_allows_hit_with_scores_and_miss():
    """Допустимые состояния конструируются без ошибок."""
    assert rt_cache.CacheLookup(hit=True, scores=RtScores(None, None)).hit is True
    assert rt_cache.CacheLookup(hit=False).scores is None


# --- TTL ---


def test_as_utc_treats_naive_as_utc():
    """SQLite-ветка (тесты): naive-значение трактуется как UTC."""
    assert rt_cache._as_utc(datetime(2026, 1, 1, 9)) == datetime(
        2026, 1, 1, 9, tzinfo=timezone.utc
    )


def test_as_utc_normalizes_aware_non_utc():
    """Продакшен-ветка (PostgreSQL TIMESTAMPTZ): aware-значение приводится к UTC."""
    moment = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=3)))
    assert rt_cache._as_utc(moment) == datetime(2026, 1, 1, 9, tzinfo=timezone.utc)


def test_expired_positive_row_is_miss(app):
    """Запись старше TTL (60 дней по умолчанию) — miss."""
    rt_cache.set_scores('tt0111161', RtScores(91, 82), app=app)
    _age_row(app, 'tt0111161', 100)
    assert rt_cache.get_scores('tt0111161', app=app) == rt_cache.MISS


def test_expired_negative_row_is_miss(app):
    """Протухшая negative-запись тоже miss (TTL applies к обоим)."""
    rt_cache.set_scores('tt9999999', RtScores(None, None), app=app)
    _age_row(app, 'tt9999999', 61)
    assert rt_cache.get_scores('tt9999999', app=app) == rt_cache.MISS


def test_row_within_ttl_is_hit(app):
    """Запись моложе TTL остаётся hit."""
    rt_cache.set_scores('tt0111161', RtScores(91, 82), app=app)
    _age_row(app, 'tt0111161', 59)
    assert rt_cache.get_scores('tt0111161', app=app).hit is True


def test_custom_ttl_from_env(app, monkeypatch):
    """RT_CACHE_TTL_DAYS читается в момент вызова: TTL=1 день."""
    monkeypatch.setenv('RT_CACHE_TTL_DAYS', '1')
    rt_cache.set_scores('tt0111161', RtScores(91, 82), app=app)
    _age_row(app, 'tt0111161', 2)
    assert rt_cache.get_scores('tt0111161', app=app) == rt_cache.MISS


@pytest.mark.parametrize('raw_value', ['abc', '', '0', '-5'])
def test_invalid_ttl_falls_back_to_default(app, monkeypatch, raw_value):
    """Некорректный TTL (нечисло, < 1) —> дефолт 60, без исключений."""
    monkeypatch.setenv('RT_CACHE_TTL_DAYS', raw_value)
    rt_cache.set_scores('tt0111161', RtScores(91, 82), app=app)
    _age_row(app, 'tt0111161', 59)
    assert rt_cache.get_scores('tt0111161', app=app).hit is True
    _age_row(app, 'tt0111161', 61)
    assert rt_cache.get_scores('tt0111161', app=app) == rt_cache.MISS


def test_ttl_outside_recommended_range_is_applied(app, monkeypatch):
    """Диапазон 30–90 — рекомендация: значение 5 дней применяется без клампинга."""
    monkeypatch.setenv('RT_CACHE_TTL_DAYS', '5')
    rt_cache.set_scores('tt0111161', RtScores(91, 82), app=app)
    _age_row(app, 'tt0111161', 4)
    assert rt_cache.get_scores('tt0111161', app=app).hit is True
    _age_row(app, 'tt0111161', 6)
    assert rt_cache.get_scores('tt0111161', app=app) == rt_cache.MISS


# --- Upsert ---


def test_upsert_updates_scores_and_fetched_at(app):
    """Повторная запись обновляет оценки и fetched_at без дубля строки."""
    rt_cache.set_scores('tt0111161', RtScores(91, 82), app=app)
    _age_row(app, 'tt0111161', 10)
    rt_cache.set_scores('tt0111161', RtScores(50, 60), app=app)

    assert _row_count(app) == 1
    with app.app_context():
        row = db.session.get(RtScore, 'tt0111161')
        assert (row.rt_score, row.metascore) == (50, 60)
        age = datetime.now(timezone.utc) - rt_cache._as_utc(row.fetched_at)
        assert age < timedelta(seconds=60)
    # Протухшая запись после upsert снова актуальна
    lookup = rt_cache.get_scores('tt0111161', app=app)
    assert lookup.hit is True
    assert lookup.scores == RtScores(rt_score=50, metascore=60)


def test_upsert_can_turn_hit_into_negative(app):
    """Запись (None, None) поверх оценок сохраняет negative-кэш."""
    rt_cache.set_scores('tt0111161', RtScores(91, 82), app=app)
    rt_cache.set_scores('tt0111161', RtScores(None, None), app=app)
    lookup = rt_cache.get_scores('tt0111161', app=app)
    assert lookup.hit is True
    assert lookup.scores == RtScores(rt_score=None, metascore=None)
    assert lookup.is_negative is True


def test_set_with_none_scores_is_noop(app):
    """scores=None (сбой OMDb/нет ответа) — no-op: строка не создаётся.

    Контракт защищает от «залипания» отрицательного кэша на TTL: сбой
    источника не должен кэшироваться как «оценок нет».
    """
    rt_cache.set_scores('tt0111161', None, app=app)
    assert _row_count(app) == 0
    assert rt_cache.get_scores('tt0111161', app=app) == rt_cache.MISS


def test_set_with_none_scores_does_not_overwrite_existing_row(app):
    """scores=None не трогает уже накопленную запись (ни оценки, ни fetched_at)."""
    rt_cache.set_scores('tt0111161', RtScores(91, 82), app=app)
    _age_row(app, 'tt0111161', 10)
    rt_cache.set_scores('tt0111161', None, app=app)
    lookup = rt_cache.get_scores('tt0111161', app=app)
    assert lookup.scores == RtScores(rt_score=91, metascore=82)


# --- Feature flag ---


def test_flag_off_get_returns_miss_without_db(monkeypatch):
    """Флаг выключен — чтение возвращает MISS без обращения к БД (сценарий S6.1).

    След вызова фиксируется ДО raise: get_scores обёрнут в except Exception,
    а AssertionError — наследник Exception, поэтому проглоченное исключение
    само по себе тест не уронило бы. Проверяем именно отсутствие обращения.
    """
    monkeypatch.setenv('ENABLE_RT_SCORES', 'false')
    calls: list = []

    def _boom() -> Flask:
        calls.append('db')
        raise AssertionError('Не должно быть обращения к БД при выключенном флаге')

    monkeypatch.setattr(rt_cache, '_get_app', _boom)
    assert rt_cache.get_scores('tt0111161') == rt_cache.MISS
    assert calls == []


def test_flag_off_set_is_noop(app, monkeypatch):
    """Флаг выключен — запись не производит строк в БД."""
    monkeypatch.setenv('OMDB_API_KEY', '')
    rt_cache.set_scores('tt0111161', RtScores(91, 82), app=app)
    assert _row_count(app) == 0


def test_flag_off_set_returns_early_without_db(monkeypatch):
    """Флаг выключен — запись не обращается к БД (сценарий S6.2)."""
    monkeypatch.setenv('ENABLE_RT_SCORES', 'false')
    calls: list = []

    def _boom() -> Flask:
        calls.append('db')
        raise AssertionError('Не должно быть обращения к БД при выключенном флаге')

    monkeypatch.setattr(rt_cache, '_get_app', _boom)
    rt_cache.set_scores('tt0111161', RtScores(91, 82))
    assert calls == []


def test_empty_imdb_id_is_miss_and_noop(app):
    """Пустой/None IMDb ID — MISS при чтении и no-op при записи."""
    assert rt_cache.get_scores(None, app=app) == rt_cache.MISS
    assert rt_cache.get_scores('   ', app=app) == rt_cache.MISS
    rt_cache.set_scores(None, RtScores(91, 82), app=app)
    rt_cache.set_scores('', RtScores(91, 82), app=app)
    assert _row_count(app) == 0


# --- Fail-silent при недоступной БД ---


def test_db_unavailable_get_returns_miss(broken_app, caplog):
    """БД недоступна при чтении — MISS, исключение не пробрасывается."""
    with caplog.at_level(logging.WARNING, logger='rt_cache'):
        lookup = rt_cache.get_scores('tt0111161', app=broken_app)
    assert lookup == rt_cache.MISS
    assert 'Не удалось прочитать кэш RT-оценок' in caplog.text


def test_db_unavailable_set_does_not_raise(broken_app, caplog):
    """БД недоступна при записи — no-op, исключение не пробрасывается (S5.2)."""
    with caplog.at_level(logging.WARNING, logger='rt_cache'):
        rt_cache.set_scores('tt0111161', RtScores(91, 82), app=broken_app)
    assert 'Не удалось записать кэш RT-оценок' in caplog.text


def test_db_failure_warning_logged_once(broken_app, caplog):
    """Первый сбой — warning, последующие — debug (без нарастания шума)."""
    with caplog.at_level(logging.DEBUG, logger='rt_cache'):
        rt_cache.get_scores('tt0111161', app=broken_app)
        rt_cache.get_scores('tt0111162', app=broken_app)
    records = [r for r in caplog.records if r.name == 'rt_cache']
    warnings = [r for r in records if r.levelno == logging.WARNING]
    debugs = [r for r in records if r.levelno == logging.DEBUG]
    assert len(warnings) == 1
    assert len(debugs) >= 1


# --- Минимальное приложение кэша: синглтон, блокировка, пул соединений ---


def test_minimal_app_pool_limits_depend_on_dialect(monkeypatch):
    """ENGINE_OPTIONS (pool_size/max_overflow/pool_pre_ping) — только для PostgreSQL.

    Ограниченный пул нужен под параллельное обогащение в B5 (asyncio.gather ->
    to_thread); для SQLite эти параметры неприменимы и не задаются.
    Подключения к БД здесь нет: create_engine ленив.
    """
    monkeypatch.setenv('DATABASE_URL', 'postgresql://kinobot:kinobot@127.0.0.1:1/kinobot_db')
    pg_app = rt_cache._build_app()
    assert pg_app.config['SQLALCHEMY_ENGINE_OPTIONS'] == {
        'pool_size': 5,
        'max_overflow': 10,
        'pool_pre_ping': True,
    }
    assert pg_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] is False

    monkeypatch.setenv('DATABASE_URL', 'sqlite:///:memory:')
    sqlite_app = rt_cache._build_app()
    # Flask-SQLAlchemy проставляет пустой словарь по умолчанию (setdefault):
    # для SQLite наши параметры пула не задаются
    assert sqlite_app.config['SQLALCHEMY_ENGINE_OPTIONS'] == {}

    _dispose_app(pg_app)
    _dispose_app(sqlite_app)


def test_get_app_is_singleton_under_parallel_calls(monkeypatch):
    """Параллельные вызовы _get_app создают одно приложение (double-checked locking).

    B5 читает кэш из нескольких потоков одновременно: без блокировки первый
    всплеск создал бы несколько приложений и несколько пулов соединений.
    """
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///:memory:')
    builds: list = []
    original_build = rt_cache._build_app
    workers = 8
    barrier = threading.Barrier(workers)
    results: list = []

    def _tracked_build() -> Flask:
        builds.append(threading.current_thread().name)
        return original_build()

    monkeypatch.setattr(rt_cache, '_build_app', _tracked_build)

    def _worker() -> None:
        barrier.wait()
        # list.append атомарен в CPython — отдельная блокировка не нужна
        results.append(rt_cache._get_app())

    threads = [threading.Thread(target=_worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(builds) == 1
    assert len(results) == workers
    assert all(built is results[0] for built in results)
    _dispose_app(results[0])


# --- Асинхронные обёртки (asyncio.to_thread, StaticPool из фикстуры) ---


def test_async_roundtrip(app):
    """aset_scores + aget_scores: hit с записанными оценками."""
    asyncio.run(rt_cache.aset_scores('tt0111161', RtScores(88, 77), app=app))
    lookup = asyncio.run(rt_cache.aget_scores('tt0111161', app=app))
    assert lookup.hit is True
    assert lookup.scores == RtScores(rt_score=88, metascore=77)
    assert lookup.is_negative is False


def test_async_negative_hit(app):
    """Асинхронное чтение negative-записи — hit без оценок."""
    asyncio.run(rt_cache.aset_scores('tt9999999', RtScores(None, None), app=app))
    lookup = asyncio.run(rt_cache.aget_scores('tt9999999', app=app))
    assert lookup.hit is True
    assert lookup.scores == RtScores(rt_score=None, metascore=None)
    assert lookup.is_negative is True


def test_async_set_with_none_scores_is_noop(app):
    """Асинхронная запись scores=None (сбой источника) — no-op."""
    asyncio.run(rt_cache.aset_scores('tt0111161', None, app=app))
    assert _row_count(app) == 0


def test_async_flag_off_returns_miss_without_db(monkeypatch):
    """Флаг выключен — асинхронное чтение немедленно возвращает MISS (S7.2).

    След вызова фиксируется ДО raise: AssertionError проглатывается контуром
    fail-silent, поэтому доказательством служит пустой список calls.
    """
    monkeypatch.setenv('ENABLE_RT_SCORES', 'false')
    calls: list = []

    def _boom() -> Flask:
        calls.append('db')
        raise AssertionError('Не должно быть обращения к БД при выключенном флаге')

    monkeypatch.setattr(rt_cache, '_get_app', _boom)
    assert asyncio.run(rt_cache.aget_scores('tt0111161')) == rt_cache.MISS
    assert calls == []


def test_async_db_unavailable_returns_miss(broken_app):
    """БД недоступна — асинхронное чтение возвращает MISS без исключений."""
    assert asyncio.run(rt_cache.aget_scores('tt0111161', app=broken_app)) == rt_cache.MISS
