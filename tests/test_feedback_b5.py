"""Тесты B5: обратная связь нейтральными эмодзи (изменение add-movie-feedback).

Покрывают:
- модель `MovieFeedback`: состав колонок, уникальность (user_id,
  kinopoisk_id), CHECK-ограничение диапазона оценки, timezone-aware
  created_at/updated_at с default+server_default (+onupdate у updated_at),
  создание таблицы `db.create_all()` и отклонение дубля/мусорной оценки на
  уровне БД (in-memory SQLite), единственный источник схемы (init_db.py
  без сырого SQL);
- `FeedbackManager`: переносимый upsert (select-then-update/insert +
  IntegrityError → rollback → повтор как UPDATE), fail-silent при
  недоступности БД (без исключений, warning однократно), валидация
  значений ДО записи, интеграция с реальной SQLite (одна строка на пару,
  сосуществование реакции и оценки, сброс оценки, изоляция пользователей,
  сортировка list_user_feedback по updated_at DESC), ленивый синглтон;
- рендер: ряд фидбека карточки («⭐ Оценить»/«✅ Смотрел»/«❌ Не моё»,
  `fb:rate:{id}`/`fb:watched:{id}`/`fb:nope:{id}`) — ВСЕГДА нижний ряд,
  callback_data ≤ 64 байт, сборка БЕЗ обращения к БД (падающий менеджер
  не ломает карточку); `build_feedback_rating_keyboard`: два ряда цифр
  1–5/6–10 (`fb:score:{id}:{n}`, обычные цифры, не эмодзи), «🗑️ Сбросить
  оценку» (`fb:clear:{id}`) и «⬅️ Назад» (`info:{id}`);
- callback-маршруты бота: watched/nope/score/clear — запись через
  менеджер и подтверждение РЕДАКТИРОВАНИЕМ («✅ Сохранено.» / «✅ Оценка
  сброшена.») с выходами «🎬 Карточка»/«⬅️ К списку»; rate — панель без
  БД; некорректные данные — дружелюбное объяснение БЕЗ записи; сбой
  хранилища — честная ошибка generic с `retry:fb:…`, НЕ «Сохранено»;
  повторный тап — одна строка (upsert, реальная SQLite); retry:fb:…,
  неизвестное действие fb:, пользователь не определён.

Все проверки офлайн: фейки PTB из tests/conftest.py, БД — моки или
in-memory SQLite, сеть и реальный Telegram не используются.
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest
from flask import Flask
from sqlalchemy import CheckConstraint, UniqueConstraint, inspect
from sqlalchemy.exc import IntegrityError

import feedback_manager as feedback_manager_module
import models.database as db_module
import telegram_bot
from conftest import (
    FakeCallbackUpdate,
    FakeUser,
    all_buttons as _all_buttons,
    editable_message as _editable_message,
    install_callback_mocks as _install_callback_mocks,
    make_manager as _manager,
    make_movie,
    run_coro as _run,
)
from dialogue_manager import (
    FEEDBACK_BACK_BUTTON_TEXT,
    FEEDBACK_CARD_BUTTON_TEXT,
    FEEDBACK_CLEAR_BUTTON_TEXT,
    FEEDBACK_NOPE_BUTTON_TEXT,
    FEEDBACK_RATE_BUTTON_TEXT,
    FEEDBACK_RATING_PROMPT,
    FEEDBACK_WATCHED_BUTTON_TEXT,
    build_feedback_rating_keyboard,
    build_movie_card_keyboard,
)
from feedback_manager import FeedbackManager, get_feedback_manager
from models.database import MovieFeedback

ROOT = Path(__file__).resolve().parents[1]


# --- Общие хелперы ---


class FakeFeedbackManager:
    """In-memory заглушка FeedbackManager для callback-тестов бота."""

    def __init__(self) -> None:
        self.reaction_calls: List[Any] = []
        self.rating_calls: List[Any] = []
        self.clear_calls: List[Any] = []
        self.reaction_result = True
        self.rating_result = True
        self.clear_result = True

    def set_reaction(self, user_id, kinopoisk_id, reaction):
        self.reaction_calls.append((user_id, kinopoisk_id, reaction))
        return self.reaction_result

    def set_rating(self, user_id, kinopoisk_id, rating):
        self.rating_calls.append((user_id, kinopoisk_id, rating))
        return self.rating_result

    def clear_rating(self, user_id, kinopoisk_id):
        self.clear_calls.append((user_id, kinopoisk_id))
        return self.clear_result


def _install_feedback(monkeypatch, fake) -> None:
    monkeypatch.setattr(telegram_bot, 'get_feedback_manager', lambda: fake)


def _dispatch(monkeypatch, dm, data: str, message=None, from_user=FakeUser(777)):
    """Прогнать callback через диспетчер с подменёнными менеджером и фидбеком."""
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    msg = message if message is not None else _editable_message(photo=())
    update = FakeCallbackUpdate(data, message=msg, from_user=from_user)
    _run(telegram_bot.handle_movie_detail(update, None))
    return msg, update


# === 1. Модель MovieFeedback и единственный источник схемы (п.5.1) ===


def test_movie_feedback_model_schema():
    """Колонки, nullable, уникальность пары и CHECK-диапазон — по спеке B5."""
    assert MovieFeedback.__tablename__ == 'movie_feedback'

    cols = MovieFeedback.__table__.columns
    assert cols['id'].primary_key is True
    assert cols['user_id'].nullable is False
    assert cols['user_id'].index is True
    assert cols['kinopoisk_id'].nullable is False
    # reaction/rating — независимые колонки одного состояния, NULL допустим
    assert cols['reaction'].nullable is True
    assert cols['rating'].nullable is True
    for name in ('created_at', 'updated_at'):
        assert cols[name].nullable is False
        # timezone-aware колонка с default и server_default (образец RtScore)
        assert cols[name].type.timezone is True
        assert cols[name].default is not None
        assert cols[name].server_default is not None
    # updated_at обновляется при каждом изменении строки (свежесть для C5)
    assert cols['updated_at'].onupdate is not None

    uniques = [c for c in MovieFeedback.__table_args__ if isinstance(c, UniqueConstraint)]
    assert len(uniques) == 1
    assert uniques[0].name == 'unique_user_movie_feedback'
    assert {col.name for col in uniques[0].columns} == {'user_id', 'kinopoisk_id'}

    checks = [c for c in MovieFeedback.__table_args__ if isinstance(c, CheckConstraint)]
    assert len(checks) == 1
    assert checks[0].name == 'movie_feedback_rating_range'
    assert 'rating' in str(checks[0].sqltext)


@pytest.fixture()
def sqlite_feedback_app():
    """Реальная in-memory SQLite: create_all, уникальность пары и CHECK."""
    db = db_module.db
    app = Flask('test_feedback_sqlite')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_create_all_creates_movie_feedback_and_rejects_bad_rows(sqlite_feedback_app):
    """create_all создаёт таблицу; дубль пары и оценка вне 1–10 отклоняются БД."""
    db = db_module.db
    with sqlite_feedback_app.app_context():
        assert 'movie_feedback' in inspect(db.engine).get_table_names()
        db.session.add(MovieFeedback(user_id='u1', kinopoisk_id=1, reaction='watched'))
        db.session.commit()
        # Другой пользователь с тем же фильмом — допустим
        db.session.add(MovieFeedback(user_id='u2', kinopoisk_id=1, reaction='nope'))
        db.session.commit()
        # Тот же пользователь и тот же фильм — IntegrityError (вторая строка не создаётся)
        db.session.add(MovieFeedback(user_id='u1', kinopoisk_id=1, rating=5))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        assert db.session.query(MovieFeedback).filter_by(user_id='u1', kinopoisk_id=1).count() == 1
        # CHECK-ограничение диапазона соблюдается и в SQLite (переносимость)
        db.session.add(MovieFeedback(user_id='u1', kinopoisk_id=2, rating=11))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_init_db_has_no_raw_sql_and_mentions_movie_feedback():
    """init_db.py: схема НЕ дублируется сырым SQL, таблица создаётся create_all."""
    text = (ROOT / 'init_db.py').read_text(encoding='utf-8')
    assert 'CREATE TABLE' not in text.upper()
    assert 'db.create_all()' in text
    assert 'movie_feedback' in text.lower()


# === 2. FeedbackManager на моках БД (п.5.3, паттерн test_watchlist_b4) ===


def _install_fake_db(monkeypatch, fake_model):
    from unittest.mock import MagicMock

    fake_db = MagicMock()
    monkeypatch.setattr(db_module, 'db', fake_db)
    monkeypatch.setattr(db_module, 'MovieFeedback', fake_model)
    return fake_db


def _mock_manager(monkeypatch, fake_model):
    fake_db = _install_fake_db(monkeypatch, fake_model)
    return FeedbackManager(app=Flask('test_feedback_mocks')), fake_db


def test_manager_set_reaction_inserts_new_row(monkeypatch):
    from unittest.mock import MagicMock

    fake_model = MagicMock()
    manager, fake_db = _mock_manager(monkeypatch, fake_model)
    fake_db.session.query.return_value.filter_by.return_value.first.return_value = None

    assert manager.set_reaction('u1', 5, 'watched') is True

    fake_model.assert_called_once_with(user_id='u1', kinopoisk_id=5, reaction='watched')
    fake_db.session.add.assert_called_once_with(fake_model.return_value)
    fake_db.session.commit.assert_called_once()


def test_manager_set_rating_updates_existing_row(monkeypatch):
    from unittest.mock import MagicMock

    fake_model = MagicMock()
    manager, fake_db = _mock_manager(monkeypatch, fake_model)
    existing = MagicMock()
    fake_db.session.query.return_value.filter_by.return_value.first.return_value = existing

    assert manager.set_rating('u1', 5, 8) is True

    assert existing.rating == 8
    fake_db.session.add.assert_not_called()
    fake_db.session.commit.assert_called_once()


def test_manager_invalid_values_rejected_without_db(monkeypatch):
    """Некорректные значения — False БЕЗ обращения к БД (защита от мусора)."""
    from unittest.mock import MagicMock

    fake_model = MagicMock()
    manager, fake_db = _mock_manager(monkeypatch, fake_model)

    assert manager.set_reaction('u1', 5, 'like') is False
    assert manager.set_rating('u1', 5, 11) is False
    assert manager.set_rating('u1', 5, 0) is False
    assert manager.set_rating('u1', 5, True) is False  # bool — не оценка
    assert manager.set_rating('u1', 5, '8') is False   # type: ignore[arg-type]
    fake_db.session.query.assert_not_called()


def test_manager_upsert_race_integrity_error_retries_as_update(monkeypatch):
    """Гонка двух тапов: IntegrityError → rollback → повтор как UPDATE, True."""
    from unittest.mock import MagicMock

    fake_model = MagicMock()
    manager, fake_db = _mock_manager(monkeypatch, fake_model)
    existing = MagicMock()
    # Первая попытка — строки ещё нет (insert), после гонки — уже есть (update)
    fake_db.session.query.return_value.filter_by.return_value.first.side_effect = [None, existing]
    fake_db.session.commit.side_effect = [
        IntegrityError('INSERT INTO movie_feedback', {}, Exception('duplicate key value violates unique constraint "unique_user_movie_feedback"')),
        None,
    ]

    assert manager.set_reaction('u1', 5, 'watched') is True

    fake_db.session.rollback.assert_called_once()
    assert existing.reaction == 'watched'


def test_manager_db_failure_is_fail_silent(monkeypatch, caplog):
    """БД недоступна: все методы возвращают безопасные значения, warning в логе."""
    from unittest.mock import MagicMock

    fake_model = MagicMock()
    manager, fake_db = _mock_manager(monkeypatch, fake_model)
    fake_db.session.query.side_effect = RuntimeError('connection refused')
    fake_model.query.filter_by.side_effect = RuntimeError('connection refused')

    with caplog.at_level(logging.WARNING, logger='feedback_manager'):
        assert manager.set_reaction('u1', 5, 'watched') is False
        assert manager.set_rating('u1', 5, 8) is False
        assert manager.clear_rating('u1', 5) is False
        assert manager.get_feedback('u1', 5) is None
        assert manager.list_user_feedback('u1') == []
    assert any('фидбек' in r.getMessage().lower() for r in caplog.records)


def test_manager_clear_rating_sets_none_and_tolerates_missing_row(monkeypatch):
    from unittest.mock import MagicMock

    fake_model = MagicMock()
    manager, fake_db = _mock_manager(monkeypatch, fake_model)
    existing = MagicMock()
    existing.rating = 7
    chain = fake_db.session.query.return_value.filter_by.return_value
    chain.first.return_value = existing

    assert manager.clear_rating('u1', 5) is True
    assert existing.rating is None
    fake_db.session.commit.assert_called_once()

    # Строки нет — нечего сбрасывать: штатный True без записи
    chain.first.return_value = None
    assert manager.clear_rating('u1', 6) is True
    fake_db.session.add.assert_not_called()


def test_manager_get_feedback_returns_detached_dict(monkeypatch):
    from unittest.mock import MagicMock

    fake_model = MagicMock()
    _install_fake_db(monkeypatch, fake_model)
    row = SimpleNamespace(kinopoisk_id=5, reaction='watched', rating=8, created_at=None, updated_at=None)
    fake_model.query.filter_by.return_value.first.return_value = row
    manager = FeedbackManager(app=Flask('test'))

    assert manager.get_feedback('u1', 5) == {
        'kinopoisk_id': 5, 'reaction': 'watched', 'rating': 8,
        'created_at': None, 'updated_at': None,
    }
    fake_model.query.filter_by.assert_called_with(user_id='u1', kinopoisk_id=5)


def test_manager_list_user_feedback_filters_and_orders(monkeypatch):
    from unittest.mock import MagicMock

    fake_model = MagicMock()
    _install_fake_db(monkeypatch, fake_model)
    rows = [
        SimpleNamespace(kinopoisk_id=6, reaction=None, rating=9, created_at=None, updated_at=None),
        SimpleNamespace(kinopoisk_id=7, reaction='nope', rating=None, created_at=None, updated_at=None),
    ]
    fake_model.query.filter_by.return_value.order_by.return_value.all.return_value = rows
    manager = FeedbackManager(app=Flask('test'))

    items = manager.list_user_feedback('u1')

    assert [it['kinopoisk_id'] for it in items] == [6, 7]
    assert items[1] == {'kinopoisk_id': 7, 'reaction': 'nope', 'rating': None, 'created_at': None, 'updated_at': None}
    fake_model.query.filter_by.assert_called_with(user_id='u1')
    fake_model.query.filter_by.return_value.order_by.assert_called_once()


def test_get_feedback_manager_is_lazy_singleton(monkeypatch):
    """Модульный синглтон: один FeedbackManager на процесс (ленивый)."""
    monkeypatch.setattr(feedback_manager_module, '_manager', None)

    first = get_feedback_manager()
    second = get_feedback_manager()

    assert first is second
    assert isinstance(first, FeedbackManager)
    monkeypatch.setattr(feedback_manager_module, '_manager', None)


# === 2b. FeedbackManager на реальной SQLite (интеграция, п.5.3) ===


def test_manager_sqlite_upsert_keeps_single_row(sqlite_feedback_app):
    """Повторные тапы и смена значений — ОДНА строка на пару (критерий B5)."""
    manager = FeedbackManager(app=sqlite_feedback_app)

    assert manager.set_reaction('u1', 1, 'watched') is True
    assert manager.set_reaction('u1', 1, 'watched') is True   # идемпотентно
    assert manager.set_reaction('u1', 1, 'nope') is True      # смена реакции
    assert manager.set_rating('u1', 1, 8) is True
    assert manager.set_rating('u1', 1, 9) is True             # смена оценки

    db = db_module.db
    with sqlite_feedback_app.app_context():
        assert db.session.query(MovieFeedback).filter_by(user_id='u1', kinopoisk_id=1).count() == 1
    # Реакция и оценка сосуществуют в одной строке
    feedback = manager.get_feedback('u1', 1)
    assert feedback is not None
    assert feedback['reaction'] == 'nope'
    assert feedback['rating'] == 9


def test_manager_sqlite_clear_rating_keeps_reaction(sqlite_feedback_app):
    manager = FeedbackManager(app=sqlite_feedback_app)
    manager.set_reaction('u1', 1, 'watched')
    manager.set_rating('u1', 1, 8)

    assert manager.clear_rating('u1', 1) is True
    feedback = manager.get_feedback('u1', 1)
    assert feedback is not None
    assert feedback['rating'] is None
    assert feedback['reaction'] == 'watched'  # реакция сохранена
    # Сброс без строки — штатный True
    assert manager.clear_rating('u1', 999) is True


def test_manager_sqlite_rejects_out_of_range_rating(sqlite_feedback_app):
    manager = FeedbackManager(app=sqlite_feedback_app)

    assert manager.set_rating('u1', 1, 11) is False
    assert manager.set_rating('u1', 1, 0) is False
    assert manager.get_feedback('u1', 1) is None  # мусор не записан


def test_manager_sqlite_list_user_feedback_isolated_and_sorted(sqlite_feedback_app):
    """Чужие записи не видны; сортировка — по updated_at DESC (свежие сверху)."""
    db = db_module.db
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with sqlite_feedback_app.app_context():
        db.session.add(MovieFeedback(user_id='u9', kinopoisk_id=1, reaction='watched', created_at=base, updated_at=base))
        db.session.add(MovieFeedback(user_id='u9', kinopoisk_id=2, rating=7, created_at=base, updated_at=base + timedelta(days=1)))
        db.session.add(MovieFeedback(user_id='other', kinopoisk_id=3, reaction='nope', created_at=base, updated_at=base))
        db.session.commit()
    manager = FeedbackManager(app=sqlite_feedback_app)

    items = manager.list_user_feedback('u9')
    assert [it['kinopoisk_id'] for it in items] == [2, 1]
    assert manager.list_user_feedback('nobody') == []


# === 3. Рендер: ряд фидбека и панель оценки (п.5.4) ===


def test_card_keyboard_has_feedback_row_last():
    keyboard = build_movie_card_keyboard(make_movie())

    last_row = keyboard.inline_keyboard[-1]
    assert [b.text for b in last_row] == [FEEDBACK_RATE_BUTTON_TEXT, FEEDBACK_WATCHED_BUTTON_TEXT, FEEDBACK_NOPE_BUTTON_TEXT]
    assert [b.callback_data for b in last_row] == ['fb:rate:435', 'fb:watched:435', 'fb:nope:435']
    # Все callback_data карточки (включая фидбек) в лимите Bot API
    for b in _all_buttons(keyboard):
        if b.callback_data:
            assert len(b.callback_data.encode('utf-8')) <= 64


def test_card_keyboard_renders_without_db(monkeypatch):
    """Рендер ряда фидбека НЕ обращается к хранилищу: падающий менеджер не ломает карточку."""
    def _boom():
        raise AssertionError('Рендер карточки не должен обращаться к хранилищу фидбека')

    monkeypatch.setattr(feedback_manager_module, 'get_feedback_manager', _boom)
    monkeypatch.setattr(telegram_bot, 'get_feedback_manager', _boom)

    keyboard = build_movie_card_keyboard(make_movie())

    assert [b.callback_data for b in keyboard.inline_keyboard[-1]] == ['fb:rate:435', 'fb:watched:435', 'fb:nope:435']


def test_feedback_rating_keyboard_layout():
    keyboard = build_feedback_rating_keyboard(435)
    rows = keyboard.inline_keyboard

    # Два ряда обычных цифр 1–5 и 6–10 (эмодзи-цифры закреплены за позициями выдачи)
    assert len(rows) == 3
    assert [b.text for b in rows[0]] == ['1', '2', '3', '4', '5']
    assert [b.text for b in rows[1]] == ['6', '7', '8', '9', '10']
    assert [b.callback_data for b in rows[0]] == [f'fb:score:435:{n}' for n in range(1, 6)]
    assert [b.callback_data for b in rows[1]] == [f'fb:score:435:{n}' for n in range(6, 11)]
    # Нижний ряд: сброс оценки и возврат к карточке существующим маршрутом
    assert [b.text for b in rows[2]] == [FEEDBACK_CLEAR_BUTTON_TEXT, FEEDBACK_BACK_BUTTON_TEXT]
    assert [b.callback_data for b in rows[2]] == ['fb:clear:435', 'info:435']
    for b in _all_buttons(keyboard):
        assert len(b.text) <= 64
        assert len(b.callback_data.encode('utf-8')) <= 64


# === 4. Callback-маршруты бота (п.5.5) ===


def _prepared(monkeypatch, fake: FakeFeedbackManager):
    """Обвязка диспетчера: моки диалога + подменный менеджер фидбека."""
    dm = _manager()
    _install_callback_mocks(monkeypatch, dm)
    _install_feedback(monkeypatch, fake)
    return dm


def test_feedback_watched_callback_saves_and_edits(monkeypatch):
    """fb:watched:{id}: запись реакции и «✅ Сохранено.» РЕДАКТИРОВАНИЕМ (A5)."""
    fake = FakeFeedbackManager()
    dm = _prepared(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'fb:watched:435', message=message)

    assert fake.reaction_calls == [('777', 435, 'watched')]
    message.edit_text.assert_awaited_once()
    assert message.edit_text.await_args.args[0] == telegram_bot._FEEDBACK_SAVED_TEXT
    assert message.edit_text.await_args.kwargs['parse_mode'] == 'HTML'
    markup = message.edit_text.await_args.kwargs['reply_markup']
    assert [b.callback_data for b in _all_buttons(markup)] == ['info:435', 'back:list']
    assert [b.text for b in _all_buttons(markup)] == [FEEDBACK_CARD_BUTTON_TEXT, '⬅️ К списку']
    # Новое сообщение не отправлялось (A5)
    message.reply_text.assert_not_awaited()


def test_feedback_nope_callback_saves_reaction(monkeypatch):
    fake = FakeFeedbackManager()
    dm = _prepared(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'fb:nope:435', message=message)

    assert fake.reaction_calls == [('777', 435, 'nope')]
    message.edit_text.assert_awaited_once()
    assert message.edit_text.await_args.args[0] == telegram_bot._FEEDBACK_SAVED_TEXT


def test_feedback_rate_callback_opens_panel_without_db(monkeypatch):
    """fb:rate:{id}: панель оценки редактированием, менеджер НЕ вызывается."""
    fake = FakeFeedbackManager()
    dm = _prepared(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'fb:rate:435', message=message)

    assert fake.reaction_calls == [] and fake.rating_calls == [] and fake.clear_calls == []
    message.edit_text.assert_awaited_once()
    assert message.edit_text.await_args.args[0] == FEEDBACK_RATING_PROMPT
    callbacks = [b.callback_data for b in _all_buttons(message.edit_text.await_args.kwargs['reply_markup'])]
    assert callbacks == [f'fb:score:435:{n}' for n in range(1, 11)] + ['fb:clear:435', 'info:435']


def test_feedback_score_callback_saves_rating(monkeypatch):
    fake = FakeFeedbackManager()
    dm = _prepared(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'fb:score:435:8', message=message)

    assert fake.rating_calls == [('777', 435, 8)]
    message.edit_text.assert_awaited_once()
    assert message.edit_text.await_args.args[0] == telegram_bot._FEEDBACK_SAVED_TEXT


def test_feedback_clear_callback_confirms_reset(monkeypatch):
    fake = FakeFeedbackManager()
    dm = _prepared(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'fb:clear:435', message=message)

    assert fake.clear_calls == [('777', 435)]
    message.edit_text.assert_awaited_once()
    assert message.edit_text.await_args.args[0] == telegram_bot._FEEDBACK_CLEARED_TEXT
    markup = message.edit_text.await_args.kwargs['reply_markup']
    assert [b.callback_data for b in _all_buttons(markup)] == ['info:435', 'back:list']


@pytest.mark.parametrize('data', [
    'fb:score:435:11',   # оценка вне шкалы
    'fb:score:435:0',    # оценка вне шкалы
    'fb:score:435:x',    # оценка не число
    'fb:score:abc:8',    # id не число
    'fb:score:435',      # нет сегмента оценки
    'fb:watched:abc',    # id не число
    'fb:nope:',          # пустой id
    'fb:clear:xyz',      # id не число
])
def test_feedback_invalid_data_is_friendly_without_write(monkeypatch, data):
    """Некорректные callback_data — объяснение с выходами, записи в БД нет."""
    fake = FakeFeedbackManager()
    dm = _prepared(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, data, message=message)

    assert fake.reaction_calls == [] and fake.rating_calls == [] and fake.clear_calls == []
    message.edit_text.assert_not_awaited()
    message.reply_text.assert_awaited_once()
    assert message.reply_text.await_args.args[0] == telegram_bot._ERROR_TEXT_FEEDBACK_INVALID
    assert _all_buttons(message.reply_text.await_args.kwargs['reply_markup'])  # B7: выход есть


def test_feedback_rate_invalid_id_is_friendly(monkeypatch):
    fake = FakeFeedbackManager()
    dm = _prepared(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'fb:rate:abc', message=message)

    message.reply_text.assert_awaited_once()
    assert message.reply_text.await_args.args[0] == telegram_bot._ERROR_TEXT_FEEDBACK_RATE_INVALID


def test_feedback_storage_failure_gives_error_not_saved(monkeypatch):
    """Менеджер вернул False (сбой БД) — честная ошибка с retry:fb:…, НЕ «Сохранено»."""
    fake = FakeFeedbackManager()
    fake.reaction_result = False
    dm = _prepared(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'fb:watched:435', message=message)

    message.edit_text.assert_not_awaited()
    message.reply_text.assert_awaited_once()
    assert message.reply_text.await_args.args[0] == telegram_bot._ERROR_TEXT_GENERIC
    callbacks = [b.callback_data for b in _all_buttons(message.reply_text.await_args.kwargs['reply_markup'])]
    assert 'retry:fb:watched:435' in callbacks


def test_retry_feedback_repeats_operation(monkeypatch):
    """retry:fb:… повторяет ту же операцию записи (B7)."""
    fake = FakeFeedbackManager()
    dm = _prepared(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'retry:fb:score:435:8', message=message)

    assert fake.rating_calls == [('777', 435, 8)]
    message.edit_text.assert_awaited_once()


def test_feedback_double_tap_keeps_single_row(sqlite_feedback_app, monkeypatch):
    """Повторный тап «✅ Смотрел» — одна строка (upsert на реальной SQLite)."""
    manager = FeedbackManager(app=sqlite_feedback_app)
    dm = _manager()
    _install_callback_mocks(monkeypatch, dm)
    _install_feedback(monkeypatch, manager)

    for _ in range(2):
        _dispatch(monkeypatch, dm, 'fb:watched:435', message=_editable_message(photo=()))

    db = db_module.db
    with sqlite_feedback_app.app_context():
        rows = db.session.query(MovieFeedback).filter_by(user_id='777', kinopoisk_id=435).all()
    assert len(rows) == 1
    assert rows[0].reaction == 'watched'


def test_feedback_without_user_is_friendly(monkeypatch):
    """Пользователь не определён — дружелюбный выход, записи нет."""
    fake = FakeFeedbackManager()
    dm = _prepared(monkeypatch, fake)
    message = _editable_message(photo=())
    update = FakeCallbackUpdate('fb:watched:435', message=message, from_user=None)
    update.effective_user = None
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)

    _run(telegram_bot.handle_movie_detail(update, None))

    assert fake.reaction_calls == []
    message.reply_text.assert_awaited_once()
    assert 'Не удалось определить пользователя' in message.reply_text.await_args.args[0]


@pytest.mark.parametrize('data', ['fb:foo:435', 'fb:'])
def test_feedback_unknown_action_is_friendly(monkeypatch, data):
    """Неизвестное действие fb: — «кнопка устарела» с выходами (B7)."""
    fake = FakeFeedbackManager()
    dm = _prepared(monkeypatch, fake)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, data, message=message)

    assert fake.reaction_calls == [] and fake.rating_calls == [] and fake.clear_calls == []
    message.reply_text.assert_awaited_once()
    assert message.reply_text.await_args.args[0] == telegram_bot._ERROR_TEXT_UNKNOWN_CALLBACK
    callbacks = [b.callback_data for b in _all_buttons(message.reply_text.await_args.kwargs['reply_markup'])]
    assert 'back:list' in callbacks
