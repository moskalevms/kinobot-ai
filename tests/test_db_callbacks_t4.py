# -*- coding: utf-8 -*-
"""Тесты T4 (сценарии 1+2): DB-зависимые callback'и на чистой БД и после хука T1.

Изменение add-db-callback-regression-tests-t4 (задача T4 бэклога
backlog_2026-10-04_bugfix_telegram_ui.md). Дополняет тесты T1
(test_db_schema_bootstrap_t1.py — уровень менеджеров) PTB-уровнем:
callback'и диспетчеризуются реальной точкой входа бота
`telegram_bot.handle_movie_detail` с фейками PTB из tests/conftest.py.

Проверяется:
- сценарий 1 «БД доступна, но таблиц нет» (чистая файловая SQLite):
  `fb:watched`/`fb:nope`/`fb:score` → ЧЕСТНАЯ generic-ошибка с «🔄 Повторить»
  (`retry:{data}`), НЕ молчание и НЕ «✅ Сохранено»; `menu:stats` →
  generic-ошибка с `retry:menu:stats`, а НЕ обманчивая заглушка
  «статистика пуста» (design C5 D4);
- сценарий 2 «после стартап-хука T1»: на той же БД после
  `db_bootstrap.ensure_database_schema()` те же callback'и проходят штатно —
  подтверждение «✅ Сохранено», записи реально читаются из БД, E2E
  «ошибка → хук → retry:{data} → успех», сводка `menu:stats` с данными.
  Эти тесты краснеют при удалении/поломке хука (критерий приёмки T4
  «падают без фикса T1», design.md D3 изменения).

Реальный PostgreSQL/Telegram/сеть НЕ нужны: файловая SQLite в tmp_path
(хук и менеджеры создают РАЗНЫЕ Flask-приложения/соединения, поэтому
in-memory SQLite нельзя — design D6 изменения T1) и моки PTB.
`track_client_request` всегда подменяется: statistics_tracker кэширует
собственное Flask-приложение на процесс (design D2 изменения).
"""
import warnings
from datetime import date
from typing import Any, List, Set

import pytest
from flask import Flask
from sqlalchemy import inspect

import db_bootstrap
import feedback_manager as feedback_manager_module
import models.database as db_module
import stats_manager as stats_manager_module
import telegram_bot
import watchlist_manager as watchlist_manager_module
from conftest import (
    FakeCallbackUpdate,
    FakeChat,
    FakeMessage,
    FakeUser,
    all_buttons,
    run_coro,
)
from dialogue_manager import FEEDBACK_CALLBACK_PREFIX
from feedback_manager import FeedbackManager
from models.database import UserStatistics

USER_ID = 777
MOVIE_ID = 447301


# --- Фикстуры и хелперы (design D1/D2) ---


@pytest.fixture()
def sqlite_url(tmp_path, monkeypatch) -> str:
    """URL файловой SQLite в tmp_path; DATABASE_URL подменён (паттерн T1)."""
    url = f"sqlite:///{(tmp_path / 'kinobot_t4.sqlite3').as_posix()}"
    monkeypatch.setenv('DATABASE_URL', url)
    return url


@pytest.fixture()
def clean_managers(sqlite_url: str):
    """Сброс синглтонов DB-менеджеров до и после теста.

    ДО: `_manager = None` — ленивые синглтоны создадутся заново уже на
    тестовом DATABASE_URL (иначе прежний менеджер утёк бы в чужую БД).
    ПОСЛЕ: освобождаем пул соединения менеджера (db.session.remove +
    engine.dispose) и снова обнуляем синглтон — файловые ручки SQLite не
    висят, состояние не протекает в другие тестовые модули.
    """
    modules = (feedback_manager_module, stats_manager_module, watchlist_manager_module)
    for mod in modules:
        mod._manager = None
    yield sqlite_url
    for mod in modules:
        manager = mod._manager
        if manager is not None:
            try:
                with manager._app.app_context():
                    db_module.db.session.remove()
                    db_module.db.engine.dispose()
            except Exception as exc:
                # Teardown — best-effort: сбой освобождения пула не должен
                # ронять уже прошедший тест, поэтому исключение НЕ пробрасывается.
                # Но молчать нельзя: на Windows такой сбой прячет утечку файловых
                # ручек SQLite (главный риск design D-Risk), поэтому тихая
                # деградация делается видимой через warning.
                warnings.warn(
                    f'teardown: не удалось освободить SQLite-соединения: {exc}',
                    RuntimeWarning,
                    stacklevel=2,
                )
        mod._manager = None


def _make_app(database_url: str) -> Any:
    """Минимальное приложение менеджера (паттерн T1): БЕЗ создания таблиц."""
    app = Flask(f'kinobot_t4_check_{id(database_url)}')
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db_module.db.init_app(app)
    return app


def _table_names(database_url: str) -> Set[str]:
    """Имена таблиц файловой БД (sqlalchemy.inspect), пул освобождается."""
    app = _make_app(database_url)
    with app.app_context():
        try:
            return set(inspect(db_module.db.engine).get_table_names())
        finally:
            db_module.db.engine.dispose()


def _dispatch(monkeypatch, data: str, message: Any = None):
    """Прогнать callback через диспетчер бота (учёт статистики — заглушка).

    `track_client_request` подменяется ВСЕГДА: его модульное Flask-приложение
    кэшируется на процесс и писало бы в реальную/чужую БД (design D2).
    """
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda *a, **kw: None)
    msg = message if message is not None else FakeMessage(chat=FakeChat())
    update = FakeCallbackUpdate(data, message=msg, from_user=FakeUser(USER_ID))
    run_coro(telegram_bot.handle_movie_detail(update, None))
    return msg, update


def _retry_buttons(markup: Any) -> List[Any]:
    """Кнопки «🔄 Повторить» (callback_data начинается с `retry:`)."""
    return [b for b in all_buttons(markup) if (b.callback_data or '').startswith('retry:')]


# === 1. Сценарий 1: чистая БД БЕЗ таблиц — честная ошибка, не молчание ===


# (callback_data фидбека, ожидаемый callback_data кнопки повтора)
FB_CLEAN_DB_CASES = [
    (f'{FEEDBACK_CALLBACK_PREFIX}watched:{MOVIE_ID}', f'retry:{FEEDBACK_CALLBACK_PREFIX}watched:{MOVIE_ID}'),
    (f'{FEEDBACK_CALLBACK_PREFIX}nope:{MOVIE_ID}', f'retry:{FEEDBACK_CALLBACK_PREFIX}nope:{MOVIE_ID}'),
    (f'{FEEDBACK_CALLBACK_PREFIX}score:{MOVIE_ID}:8', f'retry:{FEEDBACK_CALLBACK_PREFIX}score:{MOVIE_ID}:8'),
]


@pytest.mark.parametrize('data,retry_data', FB_CLEAN_DB_CASES)
def test_feedback_on_clean_db_shows_error_with_retry(clean_managers, monkeypatch, data, retry_data):
    """fb:watched/fb:nope/fb:score без таблиц — generic-ошибка с «🔄 Повторить».

    Критерии T4: НЕ молчание (answer + reply_text вызваны), НЕ «✅ Сохранено»
    (ложное подтверждение запрещено), повтор цели — ПОЛНЫЙ исходный
    callback_data (`retry:{data}`, ветка _record_feedback при saved=False).
    """
    msg, update = _dispatch(monkeypatch, data)

    # Индикация нажатия снята и ответ доставлен — не молчание
    assert update.callback_query.answer_calls == 1
    assert len(msg.texts) == 1, 'ожидался ровно один ответ пользователю'
    text, kwargs = msg.texts[0]
    # Честная классифицированная ошибка (generic-класс), не подтверждение
    assert text == telegram_bot._ERROR_TEXT_GENERIC
    assert telegram_bot._FEEDBACK_SAVED_TEXT not in text
    # Кнопка «🔄 Повторить» с повтором ТОЙ ЖЕ операции
    retries = [b for b in _retry_buttons(kwargs['reply_markup']) if b.callback_data == retry_data]
    assert retries, f'нет кнопки повтора с callback_data {retry_data!r}'
    assert 'Повторить' in retries[0].text
    # Callback не создаёт таблицы «за себя» — БД остаётся чистой
    assert 'movie_feedback' not in _table_names(clean_managers)


def test_menu_stats_on_clean_db_shows_generic_error_not_empty_stub(clean_managers, monkeypatch):
    """menu:stats без таблиц — generic-ошибка с retry:menu:stats, не «пусто».

    Обманчивые нули/заглушка «статистика пуста» при недоступном хранилище
    запрещены (design C5 D4): available=False — ЧЕСТНАЯ ошибка с повтором.
    """
    msg, update = _dispatch(monkeypatch, telegram_bot.STATS_CALLBACK)

    assert update.callback_query.answer_calls == 1
    assert len(msg.texts) == 1
    text, kwargs = msg.texts[0]
    assert text == telegram_bot._ERROR_TEXT_GENERIC
    # Не обманчивая заглушка «пусто» и не её фрагменты
    assert text != telegram_bot.STATS_EMPTY_TEXT
    assert telegram_bot.STATS_EMPTY_TEXT not in text
    # Повтор ТОГО ЖЕ действия меню (ветка menu: в _handle_retry_callback)
    assert any(b.callback_data == f'retry:{telegram_bot.STATS_CALLBACK}' for b in _retry_buttons(kwargs['reply_markup']))


# === 2. Сценарий 2: после стартап-хука T1 те же callback'и штатны ===


def test_feedback_after_bootstrap_succeeds_and_persists(clean_managers, monkeypatch):
    """fb:watched/fb:nope/fb:score после ensure_database_schema — успех + запись в БД.

    Критерий «падает без фикса T1»: успех утверждается ТОЛЬКО после вызова
    реального хука; без него таблицы не появятся и менеджер вернёт False —
    assert штатного текста «✅ Сохранено.» покраснеет (design D3).
    """
    sqlite_url = clean_managers
    assert db_bootstrap.ensure_database_schema() is True

    cases = [
        (f'fb:watched:{MOVIE_ID}', MOVIE_ID),
        (f'fb:nope:{MOVIE_ID + 1}', MOVIE_ID + 1),
        (f'fb:score:{MOVIE_ID + 2}:8', MOVIE_ID + 2),
    ]
    for data, movie_id in cases:
        msg, update = _dispatch(monkeypatch, data)
        assert update.callback_query.answer_calls == 1
        assert len(msg.texts) == 1
        text, kwargs = msg.texts[0]
        assert text == telegram_bot._FEEDBACK_SAVED_TEXT, f'штатное подтверждение не доставлено для {data}'
        assert telegram_bot._ERROR_TEXT_GENERIC not in text
        # Клавиатура выходов B5: «🎬 Карточка» (info:{id}) и «⬅️ К списку»
        callbacks = [b.callback_data for b in all_buttons(kwargs['reply_markup'])]
        assert f'info:{movie_id}' in callbacks
        assert 'back:list' in callbacks

    # Записи реально в БД (чтение через отдельное приложение — паттерн T1)
    stored = FeedbackManager(app=_make_app(sqlite_url))
    watched = stored.get_feedback(str(USER_ID), MOVIE_ID)
    assert watched is not None and watched['reaction'] == FeedbackManager.REACTION_WATCHED
    nope = stored.get_feedback(str(USER_ID), MOVIE_ID + 1)
    assert nope is not None and nope['reaction'] == FeedbackManager.REACTION_NOPE
    score = stored.get_feedback(str(USER_ID), MOVIE_ID + 2)
    assert score is not None and score['rating'] == 8


def test_retry_after_bootstrap_recovers_operation(clean_managers, monkeypatch):
    """E2E: ошибка на чистой БД → хук T1 → «🔄 Повторить» (retry:{data}) → успех.

    Прод-последовательность восстановления: пользователь тапает кнопку
    повтора из сообщения об ошибке, и та же операция проходит штатно —
    фикс T1 делает кнопку «🔄 Повторить» осмысленной.
    """
    data = f'fb:watched:{MOVIE_ID}'

    # 1) Чистая БД: честная ошибка с повтором (сценарий 1)
    msg_before, _ = _dispatch(monkeypatch, data)
    # Защитный гард непустоты: при регрессии «бот промолчал» тест должен
    # упасть внятным ассертом, а не IndexError на индексации texts[0][0].
    assert msg_before.texts, 'бот промолчал на чистой БД — ожидался ответ с generic-ошибкой и кнопкой повтора'
    assert msg_before.texts[0][0] == telegram_bot._ERROR_TEXT_GENERIC

    # 2) Стартап-хук T1 создаёт схему на той же БД
    assert db_bootstrap.ensure_database_schema() is True

    # 3) Пользователь нажал «🔄 Повторить» — диспетчеризация retry:{data}
    msg_after, update_after = _dispatch(monkeypatch, f'retry:{data}')
    assert update_after.callback_query.answer_calls == 1
    assert len(msg_after.texts) == 1
    assert msg_after.texts[0][0] == telegram_bot._FEEDBACK_SAVED_TEXT


def test_menu_stats_after_bootstrap_shows_summary(clean_managers, monkeypatch):
    """menu:stats после хука и записи данных — HTML-сводка, не ошибка/заглушка.

    Обращения бота пишутся строкой user_statistics напрямую: учёт
    `track_client_request` в тестах подменён (его процессный кэш приложения
    не управляется фикстурой, design D2).
    """
    sqlite_url = clean_managers
    assert db_bootstrap.ensure_database_schema() is True

    app = _make_app(sqlite_url)
    with app.app_context():
        db_module.db.session.add(UserStatistics(session_id=f'tg:{USER_ID}', date=date(2026, 10, 4), queries_count=4))
        db_module.db.session.commit()

    # Фидбек до сводки: отметка «Смотрел» и оценка 9
    _dispatch(monkeypatch, f'fb:watched:{MOVIE_ID}')
    _dispatch(monkeypatch, f'fb:score:{MOVIE_ID + 1}:9')

    msg, update = _dispatch(monkeypatch, telegram_bot.STATS_CALLBACK)
    assert update.callback_query.answer_calls == 1
    assert len(msg.texts) == 1
    text, _kwargs = msg.texts[0]
    # Не ошибка хранилища и не обманчивая пустая заглушка
    assert text != telegram_bot._ERROR_TEXT_GENERIC
    assert telegram_bot.STATS_EMPTY_TEXT not in text
    # Сводка с данными: заголовок и строки ненулевых показателей (C5)
    assert telegram_bot.STATS_HEADER in text
    assert 'Запросов к боту: <b>4</b>' in text
    assert 'Отметок «Смотрел»: <b>1</b>' in text
    assert 'Оценок фильмов: <b>1</b>' in text
    assert '9.0' in text
