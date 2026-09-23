"""Тесты B4: watchlist «📌 Мой список» (изменение add-watchlist).

Покрывают:
- модель `Watchlist`: состав колонок, уникальность (user_id, kinopoisk_id),
  создание таблицы `db.create_all()` и отклонение дубля на уровне БД
  (in-memory SQLite), единственный источник схемы (init_db.py без сырого SQL);
- `WatchlistManager`: add/remove/contains/list_page на моках БД (паттерн
  test_session_manager), идемпотентный дубль через IntegrityError → False +
  rollback, fail-silent при недоступности БД (без исключений), интеграция
  с реальной SQLite (пагинация, сортировка по added_at DESC);
- клавиатура карточки: кнопка «📌 Сохранить» (`save:{id}`);
- `render_watchlist_page`: строки «N. <b>название</b> (год)» с html.escape,
  кнопки «🗑️ Удалить» (`unsave:{id}`), «⬇️ Ещё 5» (`wpage:{offset}`) только
  при наличии продолжения, пустой список — заглушка с CTA (B7, без dead-end);
- callback-маршруты бота: save (успех/дубль/сбой хранилища/нет фильма в
  сессии/некорректный id), unsave (удаление + перерендер), watchlist:view,
  wpage (включая устаревший offset), retry:save;
- точки входа: команда /list, пункт reply-меню «📌 Мой список», состав
  главного меню и BotCommand.

Все проверки офлайн: фейки PTB/aiohttp из tests/conftest.py, БД — моки или
in-memory SQLite, сеть и реальный Telegram не используются.
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import MagicMock

import pytest
from flask import Flask
from sqlalchemy import UniqueConstraint, inspect
from sqlalchemy.exc import IntegrityError

import models.database as db_module
import telegram_bot
from conftest import (
    POSTER_URL,
    FakeCallbackUpdate,
    FakeChat,
    FakeMessage,
    FakeUser,
    all_buttons as _all_buttons,
    editable_message as _editable_message,
    install_callback_mocks as _install_callback_mocks,
    make_manager as _manager,
    make_movie,
    run_coro as _run,
)
from dialogue_manager import (
    CARD_SAVE_BUTTON_TEXT,
    RANDOM_MOVIE_CALLBACK,
    UNSAVE_CALLBACK_PREFIX,
    WATCHLIST_CALLBACK,
    WATCHLIST_EMPTY_TEXT,
    WATCHLIST_HEADER,
    WATCHLIST_MORE_BUTTON_TEXT,
    WATCHLIST_PAGE_LIMIT,
    WATCHLIST_PAGE_PREFIX,
    WATCHLIST_REMOVE_BUTTON_TEXT,
    build_movie_card_keyboard,
    render_watchlist_page,
)
from models.database import Watchlist
from watchlist_manager import WatchlistManager

ROOT = Path(__file__).resolve().parents[1]


# --- Общие хелперы ---


def _items(count: int, start: int = 1) -> List[dict]:
    """Элементы watchlist в форме, которую возвращает WatchlistManager.list_page."""
    return [
        {
            'kinopoisk_id': i,
            'title': f'Фильм {i}',
            'year': 2000 + i,
            'poster_url': None,
            'added_at': None,
        }
        for i in range(start, start + count)
    ]


class FakeWatchlistManager:
    """In-memory заглушка WatchlistManager для callback-тестов бота."""

    def __init__(self) -> None:
        self.add_calls: List[Any] = []
        self.remove_calls: List[Any] = []
        self.contains_calls: List[Any] = []
        self.list_calls: List[Any] = []
        self.add_result = True
        self.remove_result = True
        self.contains_result = False
        self.items: List[dict] = []
        self.total = 0

    def add(self, user_id, *, kinopoisk_id, title, year, poster_url):
        self.add_calls.append((user_id, kinopoisk_id, title, year, poster_url))
        return self.add_result

    def remove(self, user_id, kinopoisk_id):
        self.remove_calls.append((user_id, kinopoisk_id))
        return self.remove_result

    def contains(self, user_id, kinopoisk_id):
        self.contains_calls.append((user_id, kinopoisk_id))
        return self.contains_result

    def list_page(self, user_id, offset, limit):
        self.list_calls.append((user_id, offset, limit))
        return self.items[offset:offset + limit], self.total


def _install_watchlist(monkeypatch, fake: FakeWatchlistManager) -> None:
    monkeypatch.setattr(telegram_bot, 'get_watchlist_manager', lambda: fake)


def _dispatch(monkeypatch, dm, data: str, message=None, from_user=FakeUser(777)):
    """Прогнать callback через диспетчер с подменёнными менеджером и watchlist."""
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    msg = message if message is not None else _editable_message(photo=())
    update = FakeCallbackUpdate(data, message=msg, from_user=from_user)
    _run(telegram_bot.handle_movie_detail(update, None))
    return msg, update


# === 1. Модель Watchlist и единственный источник схемы ===


def test_watchlist_model_schema():
    """Колонки, nullable и уникальность (user_id, kinopoisk_id) — по спеке B4."""
    assert Watchlist.__tablename__ == 'watchlist'

    cols = Watchlist.__table__.columns
    assert cols['id'].primary_key is True
    assert cols['user_id'].nullable is False
    assert cols['user_id'].index is True
    assert cols['kinopoisk_id'].nullable is False
    assert cols['title'].nullable is False
    assert cols['year'].nullable is True
    assert cols['poster_url'].nullable is True
    assert cols['added_at'].nullable is False
    # added_at: timezone-aware колонка с default и server_default (образец RtScore)
    assert cols['added_at'].type.timezone is True
    assert cols['added_at'].server_default is not None
    assert cols['added_at'].default is not None

    uniques = [c for c in Watchlist.__table_args__ if isinstance(c, UniqueConstraint)]
    assert len(uniques) == 1
    assert uniques[0].name == 'unique_user_movie'
    assert {col.name for col in uniques[0].columns} == {'user_id', 'kinopoisk_id'}


@pytest.fixture()
def sqlite_app():
    """Реальная in-memory SQLite: проверка create_all и ограничения уникальности."""
    db = db_module.db
    app = Flask('test_watchlist_sqlite')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_create_all_creates_watchlist_and_rejects_duplicates(sqlite_app):
    """create_all создаёт таблицу; дубль (user_id, kinopoisk_id) отклоняется БД."""
    db = db_module.db
    with sqlite_app.app_context():
        assert 'watchlist' in inspect(db.engine).get_table_names()
        db.session.add(Watchlist(user_id='u1', kinopoisk_id=1, title='Дюна'))
        db.session.commit()
        # Другой пользователь с тем же фильмом — допустим
        db.session.add(Watchlist(user_id='u2', kinopoisk_id=1, title='Дюна'))
        db.session.commit()
        # Тот же пользователь и тот же фильм — IntegrityError, вторая запись не создаётся
        db.session.add(Watchlist(user_id='u1', kinopoisk_id=1, title='Дюна (копия)'))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        assert db.session.query(Watchlist).filter_by(user_id='u1', kinopoisk_id=1).count() == 1


def test_init_db_has_no_raw_sql_and_mentions_watchlist():
    """init_db.py: схема НЕ дублируется сырым SQL, таблица создаётся create_all."""
    text = (ROOT / 'init_db.py').read_text(encoding='utf-8')
    assert 'CREATE TABLE' not in text.upper()
    assert 'db.create_all()' in text
    assert 'watchlist' in text.lower()


# === 2. WatchlistManager на моках БД (паттерн test_session_manager) ===


def _install_fake_db(monkeypatch, fake_model):
    fake_db = MagicMock()
    monkeypatch.setattr(db_module, 'db', fake_db)
    monkeypatch.setattr(db_module, 'Watchlist', fake_model)
    return fake_db


def test_manager_add_commits_row(monkeypatch):
    fake_model = MagicMock()
    fake_db = _install_fake_db(monkeypatch, fake_model)
    manager = WatchlistManager(app=Flask('test'))

    assert manager.add('u1', kinopoisk_id=5, title='Дюна', year=2021, poster_url='http://p') is True

    fake_model.assert_called_once_with(
        user_id='u1', kinopoisk_id=5, title='Дюна', year=2021, poster_url='http://p'
    )
    fake_db.session.add.assert_called_once_with(fake_model.return_value)
    fake_db.session.commit.assert_called_once()


def test_manager_add_duplicate_integrity_error_returns_false(monkeypatch):
    """Дубль: IntegrityError → rollback → False, вторая запись не создаётся."""
    fake_model = MagicMock()
    fake_db = _install_fake_db(monkeypatch, fake_model)
    fake_db.session.commit.side_effect = IntegrityError(
        'INSERT INTO watchlist', {}, Exception('duplicate key value violates unique constraint "unique_user_movie"')
    )
    manager = WatchlistManager(app=Flask('test'))

    assert manager.add('u1', kinopoisk_id=5, title='Дюна', year=2021, poster_url=None) is False
    fake_db.session.rollback.assert_called_once()


def test_manager_add_db_failure_is_fail_silent(monkeypatch, caplog):
    """БД недоступна: add не кидает исключение, возвращает False, пишет warning."""
    fake_model = MagicMock()
    fake_db = _install_fake_db(monkeypatch, fake_model)
    fake_db.session.add.side_effect = RuntimeError('connection refused')
    manager = WatchlistManager(app=Flask('test'))

    with caplog.at_level(logging.WARNING, logger='watchlist_manager'):
        assert manager.add('u1', kinopoisk_id=5, title='Дюна', year=None, poster_url=None) is False
    assert any('watchlist' in r.getMessage() for r in caplog.records)


def test_manager_remove_and_contains(monkeypatch):
    fake_model = MagicMock()
    fake_db = _install_fake_db(monkeypatch, fake_model)
    filtered = fake_model.query.filter_by.return_value
    manager = WatchlistManager(app=Flask('test'))

    filtered.delete.return_value = 1
    assert manager.remove('u1', 5) is True
    fake_db.session.commit.assert_called()
    filtered.delete.return_value = 0
    assert manager.remove('u1', 5) is False

    filtered.first.return_value = object()
    assert manager.contains('u1', 5) is True
    filtered.first.return_value = None
    assert manager.contains('u1', 5) is False


def test_manager_list_page_paginates_and_detaches(monkeypatch):
    fake_model = MagicMock()
    _install_fake_db(monkeypatch, fake_model)
    rows = [
        SimpleNamespace(kinopoisk_id=6, title='Фильм 6', year=2006, poster_url=None, added_at=None),
        SimpleNamespace(kinopoisk_id=7, title='Фильм 7', year=2007, poster_url='http://p', added_at=None),
    ]
    filtered = fake_model.query.filter_by.return_value
    filtered.count.return_value = 7
    paged = filtered.order_by.return_value.offset.return_value.limit.return_value
    paged.all.return_value = rows
    manager = WatchlistManager(app=Flask('test'))

    items, total = manager.list_page('u1', 5, 5)

    assert total == 7
    assert items == [
        {'kinopoisk_id': 6, 'title': 'Фильм 6', 'year': 2006, 'poster_url': None, 'added_at': None},
        {'kinopoisk_id': 7, 'title': 'Фильм 7', 'year': 2007, 'poster_url': 'http://p', 'added_at': None},
    ]
    fake_model.query.filter_by.assert_called_with(user_id='u1')
    filtered.order_by.return_value.offset.assert_called_with(5)
    filtered.order_by.return_value.offset.return_value.limit.assert_called_with(5)

    # Отрицательный offset нормализуется в 0
    manager.list_page('u1', -3, 5)
    assert filtered.order_by.return_value.offset.call_args.args[0] == 0


def test_manager_list_page_db_failure_is_fail_silent(monkeypatch):
    fake_model = MagicMock()
    _install_fake_db(monkeypatch, fake_model)
    fake_model.query.filter_by.side_effect = RuntimeError('connection refused')
    manager = WatchlistManager(app=Flask('test'))

    assert manager.list_page('u1', 0, 5) == ([], 0)
    assert manager.remove('u1', 5) is False
    assert manager.contains('u1', 5) is False


# === 2b. WatchlistManager на реальной SQLite (интеграция) ===


def test_manager_sqlite_add_duplicate_remove(sqlite_app):
    manager = WatchlistManager(app=sqlite_app)

    assert manager.add('u1', kinopoisk_id=1, title='Дюна', year=2021, poster_url=POSTER_URL) is True
    # Дубль — False, вторая запись не создана (критерий приёмки B4)
    assert manager.add('u1', kinopoisk_id=1, title='Дюна', year=2021, poster_url=None) is False
    assert manager.contains('u1', 1) is True
    items, total = manager.list_page('u1', 0, 5)
    assert total == 1
    assert items[0]['title'] == 'Дюна' and items[0]['poster_url'] == POSTER_URL
    assert manager.remove('u1', 1) is True
    assert manager.remove('u1', 1) is False
    assert manager.contains('u1', 1) is False


def test_manager_sqlite_list_page_orders_by_added_at_desc(sqlite_app):
    """Сортировка «сначала свежие» и срез LIMIT/OFFSET на стороне БД."""
    db = db_module.db
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with sqlite_app.app_context():
        for i, kp in enumerate((10, 20, 30)):
            db.session.add(Watchlist(
                user_id='u2', kinopoisk_id=kp, title=f'Фильм {kp}', year=2000 + i,
                added_at=base + timedelta(days=i),
            ))
        db.session.commit()
    manager = WatchlistManager(app=sqlite_app)

    items, total = manager.list_page('u2', 0, 2)
    assert total == 3
    assert [it['kinopoisk_id'] for it in items] == [30, 20]

    items, total = manager.list_page('u2', 2, 2)
    assert total == 3
    assert [it['kinopoisk_id'] for it in items] == [10]

    # Чужие записи не видны
    assert manager.list_page('u3', 0, 5) == ([], 0)


# === 3. Клавиатура карточки: «📌 Сохранить» ===


def test_card_keyboard_has_save_button():
    keyboard = build_movie_card_keyboard(make_movie())

    buttons = _all_buttons(keyboard)
    save = [b for b in buttons if b.text == CARD_SAVE_BUTTON_TEXT]
    assert len(save) == 1
    assert save[0].callback_data == 'save:435'
    assert len(save[0].callback_data.encode('utf-8')) <= 64
    # Все callback_data кнопок карточки в лимите Bot API
    for b in buttons:
        if b.callback_data:
            assert len(b.callback_data.encode('utf-8')) <= 64


# === 4. render_watchlist_page ===


def test_render_empty_watchlist_has_cta():
    """Пустой список — дружелюбная заглушка с мгновенным действием (B7)."""
    text, markup = render_watchlist_page([], 0, 0)

    assert text == WATCHLIST_EMPTY_TEXT
    buttons = _all_buttons(markup)
    assert [b.callback_data for b in buttons] == [RANDOM_MOVIE_CALLBACK]


def test_render_items_rows_and_remove_buttons():
    text, markup = render_watchlist_page(_items(3), 0, 3)

    assert text.startswith(f'<strong>{WATCHLIST_HEADER}</strong>')
    assert '1. <b>Фильм 1</b> (2001)' in text
    assert '3. <b>Фильм 3</b> (2003)' in text
    buttons = _all_buttons(markup)
    assert [b.text for b in buttons] == [WATCHLIST_REMOVE_BUTTON_TEXT] * 3
    assert [b.callback_data for b in buttons] == [
        f'{UNSAVE_CALLBACK_PREFIX}1', f'{UNSAVE_CALLBACK_PREFIX}2', f'{UNSAVE_CALLBACK_PREFIX}3',
    ]
    # total == количество элементов — кнопки «⬇️ Ещё 5» нет
    assert WATCHLIST_MORE_BUTTON_TEXT not in text
    assert not any(b.callback_data and b.callback_data.startswith(WATCHLIST_PAGE_PREFIX) for b in buttons)


def test_render_more_button_only_when_has_more():
    _, markup = render_watchlist_page(_items(5), 0, 12)
    last_row = markup.inline_keyboard[-1]
    assert [b.text for b in last_row] == [WATCHLIST_MORE_BUTTON_TEXT]
    assert last_row[0].callback_data == f'{WATCHLIST_PAGE_PREFIX}{WATCHLIST_PAGE_LIMIT}'

    # Последняя страница (total == offset + len(items)) — кнопки нет
    _, markup_last = render_watchlist_page(_items(2, start=6), 5, 7)
    assert not any(
        b.callback_data and b.callback_data.startswith(WATCHLIST_PAGE_PREFIX)
        for b in _all_buttons(markup_last)
    )


def test_render_continues_numbering_from_offset():
    text, markup = render_watchlist_page(_items(5, start=6), 5, 12)

    assert '6. <b>Фильм 6</b>' in text
    assert '10. <b>Фильм 10</b>' in text
    assert markup.inline_keyboard[-1][0].callback_data == 'wpage:10'


def test_render_escapes_html_and_skips_missing_year():
    items = [
        {'kinopoisk_id': 1, 'title': '<script>&"x"</script>', 'year': None, 'poster_url': None, 'added_at': None},
    ]
    text, _ = render_watchlist_page(items, 0, 1)

    assert '&lt;script&gt;' in text and '<script>' not in text
    assert '&amp;' in text
    # Год отсутствует — часть «(год)» опущена целиком, без «(None)»
    assert '(None)' not in text and '(2001)' not in text
    assert '1. <b>&lt;script&gt;&amp;&quot;x&quot;&lt;/script&gt;</b>\n' in text


# === 5. Callback-маршруты бота ===


def test_save_callback_stores_movie_from_session(monkeypatch):
    """save:{id}: фильм берётся из session.last_movies, ответ — редактированием."""
    dm = _manager()
    dm.session_manager.get_session('777').last_movies = [make_movie()]
    _install_callback_mocks(monkeypatch, dm)
    fake = FakeWatchlistManager()
    _install_watchlist(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'save:435', message=message)

    assert fake.add_calls == [('777', 435, 'Дюна', 2021, POSTER_URL)]
    message.edit_text.assert_awaited_once()
    assert message.edit_text.await_args.args[0] == telegram_bot._SAVED_TEXT
    assert message.edit_text.await_args.kwargs['parse_mode'] == 'HTML'
    markup = message.edit_text.await_args.kwargs['reply_markup']
    assert markup.inline_keyboard[0][0].callback_data == WATCHLIST_CALLBACK
    # Новое сообщение не отправлялось (A5)
    message.reply_text.assert_not_awaited()


def test_save_callback_duplicate_says_already_in_list(monkeypatch):
    """Дубль: add → False, фильм реально в списке → «Уже в списке», без записи."""
    dm = _manager()
    dm.session_manager.get_session('777').last_movies = [make_movie()]
    _install_callback_mocks(monkeypatch, dm)
    fake = FakeWatchlistManager()
    fake.add_result = False
    fake.contains_result = True
    _install_watchlist(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'save:435', message=message)

    assert len(fake.add_calls) == 1
    assert fake.contains_calls == [('777', 435)]
    message.edit_text.assert_awaited_once()
    assert message.edit_text.await_args.args[0] == telegram_bot._ALREADY_SAVED_TEXT


def test_save_callback_storage_failure_gives_error_not_duplicate_text(monkeypatch):
    """Сбой хранилища (add=False и contains=False): честная ошибка с повтором."""
    dm = _manager()
    dm.session_manager.get_session('777').last_movies = [make_movie()]
    _install_callback_mocks(monkeypatch, dm)
    fake = FakeWatchlistManager()
    fake.add_result = False
    fake.contains_result = False
    _install_watchlist(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'save:435', message=message)

    message.edit_text.assert_not_awaited()
    message.reply_text.assert_awaited_once()
    text = message.reply_text.await_args.args[0]
    markup = message.reply_text.await_args.kwargs['reply_markup']
    assert text == telegram_bot._ERROR_TEXT_GENERIC
    assert 'retry:save:435' in [b.callback_data for b in _all_buttons(markup)]


def test_save_callback_movie_not_in_session_is_friendly(monkeypatch):
    """Фильма нет в сессии — объяснение с выходами, запись не создаётся."""
    dm = _manager()
    dm.session_manager.get_session('777').last_movies = []
    _install_callback_mocks(monkeypatch, dm)
    fake = FakeWatchlistManager()
    _install_watchlist(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'save:435', message=message)

    assert fake.add_calls == []
    message.reply_text.assert_awaited_once()
    assert message.reply_text.await_args.args[0] == telegram_bot._ERROR_TEXT_SAVE_NO_MOVIE
    assert _all_buttons(message.reply_text.await_args.kwargs['reply_markup'])


def test_save_callback_invalid_id_is_friendly(monkeypatch):
    dm = _manager()
    _install_callback_mocks(monkeypatch, dm)
    fake = FakeWatchlistManager()
    _install_watchlist(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'save:abc', message=message)

    assert fake.add_calls == []
    message.reply_text.assert_awaited_once()
    assert 'Не удалось определить фильм' in message.reply_text.await_args.args[0]


def test_retry_save_repeats_operation(monkeypatch):
    """retry:save:{id} повторяет сохранение той же операции (B7)."""
    dm = _manager()
    dm.session_manager.get_session('777').last_movies = [make_movie()]
    _install_callback_mocks(monkeypatch, dm)
    fake = FakeWatchlistManager()
    _install_watchlist(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'retry:save:435', message=message)

    assert fake.add_calls == [('777', 435, 'Дюна', 2021, POSTER_URL)]
    message.edit_text.assert_awaited_once()


def test_unsave_callback_removes_and_rerenders(monkeypatch):
    """unsave:{id}: удаление + перерисовка списка редактированием."""
    dm = _manager()
    _install_callback_mocks(monkeypatch, dm)
    fake = FakeWatchlistManager()
    fake.items = _items(2)
    fake.total = 2
    _install_watchlist(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'unsave:2', message=message)

    assert fake.remove_calls == [('777', 2)]
    assert fake.list_calls == [('777', 0, WATCHLIST_PAGE_LIMIT)]
    message.edit_text.assert_awaited_once()
    text = message.edit_text.await_args.args[0]
    assert '1. <b>Фильм 1</b>' in text and '2. <b>Фильм 2</b>' in text


def test_watchlist_view_callback_shows_first_page(monkeypatch):
    dm = _manager()
    _install_callback_mocks(monkeypatch, dm)
    fake = FakeWatchlistManager()
    fake.items = _items(3)
    fake.total = 3
    _install_watchlist(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, WATCHLIST_CALLBACK, message=message)

    assert fake.list_calls == [('777', 0, WATCHLIST_PAGE_LIMIT)]
    message.edit_text.assert_awaited_once()
    markup = message.edit_text.await_args.kwargs['reply_markup']
    assert [b.callback_data for b in _all_buttons(markup)] == ['unsave:1', 'unsave:2', 'unsave:3']


def test_watchlist_view_empty_list_shows_stub(monkeypatch):
    dm = _manager()
    _install_callback_mocks(monkeypatch, dm)
    fake = FakeWatchlistManager()
    _install_watchlist(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, WATCHLIST_CALLBACK, message=message)

    message.edit_text.assert_awaited_once()
    assert message.edit_text.await_args.args[0] == WATCHLIST_EMPTY_TEXT


def test_wpage_callback_shows_next_page(monkeypatch):
    dm = _manager()
    _install_callback_mocks(monkeypatch, dm)
    fake = FakeWatchlistManager()
    fake.items = _items(7)
    fake.total = 7
    _install_watchlist(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'wpage:5', message=message)

    assert fake.list_calls == [('777', 5, WATCHLIST_PAGE_LIMIT)]
    message.edit_text.assert_awaited_once()
    text = message.edit_text.await_args.args[0]
    assert '6. <b>Фильм 6</b>' in text and '7. <b>Фильм 7</b>' in text
    # Последняя страница — кнопки «⬇️ Ещё 5» нет
    markup = message.edit_text.await_args.kwargs['reply_markup']
    assert not any(
        b.callback_data and b.callback_data.startswith(WATCHLIST_PAGE_PREFIX)
        for b in _all_buttons(markup)
    )


def test_wpage_stale_offset_falls_back_to_first_page(monkeypatch):
    """Список сократился и offset устарел — показываем первую страницу, не заглушку."""
    dm = _manager()
    _install_callback_mocks(monkeypatch, dm)
    fake = FakeWatchlistManager()
    fake.items = _items(3)
    fake.total = 3
    _install_watchlist(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'wpage:5', message=message)

    assert fake.list_calls == [('777', 5, WATCHLIST_PAGE_LIMIT), ('777', 0, WATCHLIST_PAGE_LIMIT)]
    message.edit_text.assert_awaited_once()
    assert '1. <b>Фильм 1</b>' in message.edit_text.await_args.args[0]


def test_wpage_invalid_offset_is_friendly(monkeypatch):
    dm = _manager()
    _install_callback_mocks(monkeypatch, dm)
    fake = FakeWatchlistManager()
    _install_watchlist(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'wpage:abc', message=message)

    assert fake.list_calls == []
    message.reply_text.assert_awaited_once()
    assert 'Не удалось открыть страницу списка' in message.reply_text.await_args.args[0]


def test_watchlist_callback_without_user_is_friendly(monkeypatch):
    """Пользователь не определён — дружелюбный ответ, БД не читается."""
    dm = _manager()
    _install_callback_mocks(monkeypatch, dm)
    fake = FakeWatchlistManager()
    _install_watchlist(monkeypatch, fake)
    message = _editable_message(photo=())
    update = FakeCallbackUpdate(WATCHLIST_CALLBACK, message=message, from_user=None)
    update.effective_user = None
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)

    _run(telegram_bot.handle_movie_detail(update, None))

    assert fake.list_calls == []
    message.reply_text.assert_awaited_once()
    assert 'Не удалось определить пользователя' in message.reply_text.await_args.args[0]


# === 6. Точки входа: команда /list и reply-меню ===


class _TextMessage(FakeMessage):
    def __init__(self, text: str):
        super().__init__()
        self.text = text


def test_list_command_renders_first_page_as_new_message(monkeypatch):
    fake = FakeWatchlistManager()
    fake.items = _items(2)
    fake.total = 2
    _install_watchlist(monkeypatch, fake)
    message = FakeMessage(chat=FakeChat())
    update = SimpleNamespace(message=message, effective_user=FakeUser(777))

    _run(telegram_bot.handle_watchlist_command(update, None))

    assert fake.list_calls == [('777', 0, WATCHLIST_PAGE_LIMIT)]
    assert len(message.texts) == 1
    text, kwargs = message.texts[0]
    assert kwargs['parse_mode'] == 'HTML'
    assert '1. <b>Фильм 1</b> (2001)' in text
    assert kwargs['reply_markup'] is not None


def test_main_menu_text_shows_watchlist(monkeypatch):
    """Пункт reply-меню «📌 Мой список» диспетчеризуется по точному тексту."""
    fake = FakeWatchlistManager()
    fake.items = _items(1)
    fake.total = 1
    _install_watchlist(monkeypatch, fake)
    message = _TextMessage('📌 Мой список')
    update = SimpleNamespace(message=message, effective_user=FakeUser(777))

    _run(telegram_bot.handle_message(update, None))

    assert len(message.texts) == 1
    assert '1. <b>Фильм 1</b>' in message.texts[0][0]


def test_main_menu_contains_watchlist_button():
    menu = telegram_bot.get_main_menu()
    texts = {b.text for row in menu.keyboard for b in row}
    assert '📌 Мой список' in texts


def test_bot_commands_include_list():
    commands = {c.command: c.description for c in telegram_bot.build_bot_commands()}
    assert 'list' in commands
    assert 'список' in commands['list'].lower()
    assert len(commands['list']) <= 256
