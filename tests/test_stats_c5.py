"""Тесты C5: персональная статистика (изменение add-personal-stats-command).

Покрывают:
- `StatsManager.get_user_stats`: корректность агрегации из трёх таблиц
  (user_statistics — сумма queries_count по всем дням с префиксом `tg:`,
  movie_feedback — оценки/средняя/реакции, watchlist — счётчик), изоляция
  пользователей, «пустой» пользователь (available=True и нули), fail-silent
  при недоступности БД (available=False, без исключений, warning однократно),
  ленивый синглтон `get_stats_manager`;
- рендер `build_stats_response`: полная сводка (HTML, числа экранированы,
  теги сбалансированы) с клавиатурой выхода в хаб «🏠 Меню» (`menu:main`,
  B2, изменение add-menu-hub-exit-buttons-b1b2), частичные данные (нулевые
  строки опускаются целиком), пустые данные — дружелюбная заглушка с
  ПРЕЖНЕЙ CTA-клавиатурой (B7, без ⚠️), эмодзи-соответствие гайдлайну B6
  (📊 закреплён в docs/emoji_guideline.md этим изменением);
- хендлер `handle_stats_command`: команда /stats отвечает HTML-сводкой,
  пусто — заглушка с ≥1 действием, сбой хранилища — ЧЕСТНАЯ generic-ошибка
  с `retry:menu:stats` (не обманчивые нули), сбой доставки — ошибка с
  выходами (B7), интеграция с реальной SQLite;
- точки входа: callback `menu:stats` маршрутизируется через диспетчер к
  тому же рендеру, guard «пользователь не определён» (без dead-end),
  кнопка «📊 Статистика» в главном меню, регистрация `stats` в
  `build_bot_commands()` и `CommandHandler` в `create_telegram_app()`.

Все проверки офлайн: фейки PTB из tests/conftest.py, БД — моки или
in-memory SQLite, сеть и реальный Telegram не используются.
"""
import logging
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from flask import Flask
from telegram.ext import CommandHandler

import models.database as db_module
import stats_manager as stats_manager_module
import telegram_bot
from conftest import (
    FakeCallbackUpdate,
    FakeChat,
    FakeMessage,
    FakeSentMessage,
    FakeUser,
    all_buttons,
    assert_html_balanced,
    run_coro,
)
from models.database import MovieFeedback, UserStatistics, Watchlist
from stats_manager import StatsManager, get_stats_manager

ROOT = Path(__file__).resolve().parents[1]


# --- Общие хелперы ---


EMPTY_STATS: Dict[str, Any] = {
    'available': True, 'queries': 0, 'rated': 0,
    'avg_rating': None, 'watched': 0, 'nope': 0, 'watchlist': 0,
}

FULL_STATS: Dict[str, Any] = {
    'available': True, 'queries': 12, 'rated': 5,
    'avg_rating': 8.23, 'watched': 3, 'nope': 2, 'watchlist': 4,
}


class FakeStatsManager:
    """In-memory заглушка StatsManager для тестов хендлера и маршрутов."""

    def __init__(self, stats: Optional[Dict[str, Any]] = None) -> None:
        self.stats = dict(stats if stats is not None else EMPTY_STATS)
        self.calls: List[str] = []

    def get_user_stats(self, user_id: str) -> Dict[str, Any]:
        self.calls.append(user_id)
        return dict(self.stats)


class _FailFirstReplyMessage(FakeMessage):
    """Сообщение, у которого ПЕРВЫЙ reply_text падает (имитация сбоя доставки)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0

    async def reply_text(self, text: str, **kwargs: Any) -> FakeSentMessage:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError('Telegram отклонил сообщение')
        return await super().reply_text(text, **kwargs)


def _install_stats(monkeypatch: pytest.MonkeyPatch, fake: FakeStatsManager) -> None:
    monkeypatch.setattr(telegram_bot, 'get_stats_manager', lambda: fake)


def _command_update(message: Any = None) -> Any:
    """Message-update для команды /stats (образец test_watchlist_b4)."""
    msg = message if message is not None else FakeMessage(chat=FakeChat())
    return SimpleNamespace(message=msg, effective_user=FakeUser(777))


@pytest.fixture()
def sqlite_stats_app():
    """Реальная in-memory SQLite: create_all по моделям (образец B5)."""
    db = db_module.db
    app = Flask('test_stats_sqlite')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


# === 1. StatsManager: агрегация на реальной SQLite (п.5.1) ===


def test_get_user_stats_aggregates_all_sources_and_isolates_users(sqlite_stats_app):
    """N/Y/Z + средняя + реакции; чужие строки и веб-сессии не попадают."""
    db = db_module.db
    with sqlite_stats_app.app_context():
        # N: два дня обращений бота (сумма) + чужая веб-сессия и чужой бот-юзер
        db.session.add(UserStatistics(session_id='tg:777', date=date(2026, 9, 1), queries_count=3))
        db.session.add(UserStatistics(session_id='tg:777', date=date(2026, 9, 2), queries_count=4))
        db.session.add(UserStatistics(session_id='web-session-1', date=date(2026, 9, 1), queries_count=100))
        db.session.add(UserStatistics(session_id='tg:888', date=date(2026, 9, 1), queries_count=50))
        # Y: две оценки (7 и 9 → средняя 8.0), реакция без оценки, чужая оценка
        db.session.add(MovieFeedback(user_id='777', kinopoisk_id=1, reaction='watched', rating=7))
        db.session.add(MovieFeedback(user_id='777', kinopoisk_id=2, rating=9))
        db.session.add(MovieFeedback(user_id='777', kinopoisk_id=3, reaction='nope'))
        db.session.add(MovieFeedback(user_id='888', kinopoisk_id=4, reaction='watched', rating=1))
        # Z: два сохранённых фильма + чужой
        db.session.add(Watchlist(user_id='777', kinopoisk_id=1, title='Фильм 1', year=2001, poster_url=None))
        db.session.add(Watchlist(user_id='777', kinopoisk_id=2, title='Фильм 2', year=2002, poster_url=None))
        db.session.add(Watchlist(user_id='888', kinopoisk_id=3, title='Чужой', year=None, poster_url=None))
        db.session.commit()

    manager = StatsManager(app=sqlite_stats_app)
    stats = manager.get_user_stats('777')

    assert stats == {
        'available': True,
        'queries': 7,        # 3 + 4 по всем дням, веб-сессия и tg:888 не в счёт
        'rated': 2,          # rating IS NOT NULL: фильмы 1 и 2
        'avg_rating': 8.0,   # (7 + 9) / 2
        'watched': 1,
        'nope': 1,
        'watchlist': 2,
    }


def test_get_user_stats_unknown_user_is_empty_but_available(sqlite_stats_app):
    """Пустой пользователь: нули и available=True (НЕ сбой хранилища)."""
    manager = StatsManager(app=sqlite_stats_app)
    stats = manager.get_user_stats('nobody')
    assert stats == EMPTY_STATS


def test_get_user_stats_partial_sources(sqlite_stats_app):
    """Только обращения: остальные счётчики нулевые, средняя оценка None."""
    db = db_module.db
    with sqlite_stats_app.app_context():
        db.session.add(UserStatistics(session_id='tg:777', date=date(2026, 9, 1), queries_count=2))
        db.session.commit()
    manager = StatsManager(app=sqlite_stats_app)

    stats = manager.get_user_stats('777')

    assert stats['queries'] == 2
    assert stats['rated'] == 0
    assert stats['avg_rating'] is None
    assert stats['watchlist'] == 0
    assert stats['available'] is True


# === 2. StatsManager: fail-silent и синглтон (п.5.2) ===


def test_get_user_stats_db_failure_is_fail_silent(monkeypatch, caplog):
    """БД недоступна: available=False и нули, без исключения, warning однократно."""
    from unittest.mock import MagicMock

    fake_db = MagicMock()
    monkeypatch.setattr(db_module, 'db', fake_db)
    fake_db.session.query.side_effect = RuntimeError('connection refused')
    manager = StatsManager(app=Flask('test_stats_mocks'))

    with caplog.at_level(logging.DEBUG, logger='stats_manager'):
        stats = manager.get_user_stats('777')
        stats_again = manager.get_user_stats('777')

    assert stats['available'] is False
    assert stats['queries'] == 0 and stats['watchlist'] == 0
    assert stats_again['available'] is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, 'warning о недоступности БД обязан быть однократным'


def test_get_stats_manager_is_lazy_singleton(monkeypatch):
    """Модульный синглтон: один StatsManager на процесс (ленивый)."""
    monkeypatch.setattr(stats_manager_module, '_manager', None)

    first = get_stats_manager()
    second = get_stats_manager()

    assert first is second
    assert isinstance(first, StatsManager)
    monkeypatch.setattr(stats_manager_module, '_manager', None)


def test_session_prefix_matches_tracker_contract():
    """Префикс session_id — литерал записи track_client_request(f"tg:{user_id}")."""
    assert StatsManager.SESSION_PREFIX == 'tg:'
    source = (ROOT / 'src' / 'telegram_bot.py').read_text(encoding='utf-8')
    assert 'track_client_request(f"tg:{user_id}")' in source


# === 3. Рендер сводки (п.5.1/5.3, design.md D3/D4) ===


def test_build_stats_response_full_summary():
    """Полная сводка: заголовок, все пять строк, средняя с одним знаком + «🏠 Меню» (B2)."""
    text, markup = telegram_bot.build_stats_response(FULL_STATS)

    # B2: сводка больше не dead-end — один ряд с кнопкой выхода в хаб
    assert markup is not None
    assert len(markup.inline_keyboard) == 1
    assert [b.text for b in markup.inline_keyboard[0]] == [telegram_bot.MENU_BUTTON_TEXT]
    assert markup.inline_keyboard[0][0].callback_data == 'menu:main'
    assert f'<strong>{telegram_bot.STATS_HEADER}</strong>' in text
    assert 'Запросов к боту: <b>12</b>' in text
    assert 'Оценок фильмов: <b>5</b> (средняя — <b>8.2</b>)' in text  # 8.23 → 8.2
    assert 'Отметок «Смотрел»: <b>3</b>' in text
    assert 'Отметок «Не моё»: <b>2</b>' in text
    assert 'Фильмов в «Мой список»: <b>4</b>' in text
    assert_html_balanced(text)


def test_build_stats_response_menu_exit_keyboard_respects_limits():
    """Клавиатура выхода в хаб (B2): одна кнопка, подпись и `callback_data` в лимитах Bot API."""
    _, markup = telegram_bot.build_stats_response(FULL_STATS)

    assert markup is not None
    buttons = all_buttons(markup)
    assert len(buttons) == 1
    button = buttons[0]
    assert button.text.startswith('🏠')
    assert button.text == telegram_bot.truncate_button_text(telegram_bot.MENU_BUTTON_TEXT)
    assert len(button.text) <= 64
    assert button.callback_data == telegram_bot.MENU_MAIN_CALLBACK
    assert len(button.callback_data.encode('utf-8')) <= 64
    # Общий источник констант с watchlist (B1): импорт из dialogue_manager
    assert telegram_bot.MENU_MAIN_CALLBACK == f'{telegram_bot._MENU_PREFIX}main'


def test_build_stats_response_omits_zero_lines():
    """Частичные данные: нулевые строки опускаются ЦЕЛИКОМ (без «нулевых» строк)."""
    stats = dict(EMPTY_STATS, queries=3)
    text, markup = telegram_bot.build_stats_response(stats)

    assert 'Запросов к боту: <b>3</b>' in text
    assert 'Оценок' not in text
    assert 'Отметок' not in text
    assert '«Мой список»' not in text
    # Любая непустая сводка идёт с выходом в хаб (B2)
    assert [b.callback_data for b in all_buttons(markup)] == ['menu:main']


def test_build_stats_response_rated_without_avg():
    """Оценки есть, средняя None (защита от рассогласования источника) — без «(средняя …)»."""
    stats = dict(EMPTY_STATS, rated=2, avg_rating=None)
    text, _ = telegram_bot.build_stats_response(stats)
    assert 'Оценок фильмов: <b>2</b>' in text
    assert 'средняя' not in text


def test_build_stats_response_empty_is_stub_with_cta():
    """Пустые данные: дружелюбная заглушка (без ⚠️) + ≥1 мгновенное действие (B7)."""
    text, markup = telegram_bot.build_stats_response(EMPTY_STATS)

    assert text == telegram_bot.STATS_EMPTY_TEXT
    assert '⚠' not in text, 'заглушка не должна выглядеть как ошибка'
    assert markup is not None
    callbacks = [b.callback_data for b in all_buttons(markup)]
    assert callbacks, 'CTA-клавиатура обязана содержать хотя бы одно действие'
    assert 'random:movie' in callbacks or 'retry:top' in callbacks
    # B2 НЕ меняет пустую ветку: CTA остаётся прежним, ряда «🏠 Меню» в нём нет
    assert 'menu:main' not in callbacks
    assert callbacks == [b.callback_data for b in all_buttons(telegram_bot.build_exit_keyboard())]


def test_stats_texts_follow_emoji_guideline():
    """Тексты C5: ≤1 эмодзи на строку, запретный набор отсутствует (гайдлайн B6)."""
    forbidden = ('😂', '🤣', '🙂', '👍', '💀', '🗿')
    summary_text, _ = telegram_bot.build_stats_response(FULL_STATS)
    for text in (telegram_bot.STATS_HEADER, telegram_bot.STATS_EMPTY_TEXT, summary_text):
        for char in forbidden:
            assert char not in text
        for line in text.split('\n'):
            emoji_count = sum(
                1 for ch in line
                if ord(ch) > 0x2000
                and not 0xFE00 <= ord(ch) <= 0xFE0F
                and ch not in '—«»…'
            )
            assert emoji_count <= 1, f'больше одного эмодзи в строке: {line!r}'
    # Строки показателей сводки — без эмодзи (только заголовок 📊)
    for line in summary_text.split('\n')[1:]:
        assert not any(ord(ch) > 0x2000 and not 0xFE00 <= ord(ch) <= 0xFE0F and ch not in '—«»…' for ch in line)


def test_emoji_guideline_documents_chart_emoji():
    """📊 закреплён в гайдлайне ДО кода (чек-лист B6 п.2, design.md D5)."""
    text = (ROOT / 'docs' / 'emoji_guideline.md').read_text(encoding='utf-8')
    assert '📊' in text
    assert 'персональная статистика' in text.lower()


# === 4. Команда /stats (п.5.2/5.3) ===


def test_stats_command_renders_summary_as_html(monkeypatch):
    """/stats: HTML-сводка новым сообщением, user_id — строка из effective_user."""
    fake = FakeStatsManager(FULL_STATS)
    _install_stats(monkeypatch, fake)
    message = FakeMessage(chat=FakeChat())
    update = _command_update(message)

    run_coro(telegram_bot.handle_stats_command(update, None))

    assert fake.calls == ['777']
    assert len(message.texts) == 1
    text, kwargs = message.texts[0]
    assert kwargs['parse_mode'] == 'HTML'
    assert 'Запросов к боту: <b>12</b>' in text
    # Точка вызова НЕ игнорирует клавиатуру (риск B2): выход в хаб доставлен
    assert [b.callback_data for b in all_buttons(kwargs['reply_markup'])] == ['menu:main']


def test_stats_command_empty_data_shows_stub_with_cta(monkeypatch):
    """/stats без данных: заглушка с CTA-кнопками — не пустое сообщение, не ошибка."""
    fake = FakeStatsManager()
    _install_stats(monkeypatch, fake)
    message = FakeMessage(chat=FakeChat())
    update = _command_update(message)

    run_coro(telegram_bot.handle_stats_command(update, None))

    text, kwargs = message.texts[0]
    assert text == telegram_bot.STATS_EMPTY_TEXT
    assert '⚠' not in text
    callbacks = [b.callback_data for b in all_buttons(kwargs['reply_markup'])]
    assert callbacks
    # Пустая ветка НЕ изменилась (B2): CTA без ряда «🏠 Меню»
    assert 'menu:main' not in callbacks


def test_stats_command_db_failure_shows_honest_error_with_retry(monkeypatch):
    """available=False: ЧЕСТНАЯ generic-ошибка с retry:menu:stats, не «пусто»."""
    fake = FakeStatsManager(dict(EMPTY_STATS, available=False))
    _install_stats(monkeypatch, fake)
    message = FakeMessage(chat=FakeChat())
    update = _command_update(message)

    run_coro(telegram_bot.handle_stats_command(update, None))

    text, kwargs = message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_GENERIC
    assert telegram_bot.STATS_EMPTY_TEXT not in text
    callbacks = [b.callback_data for b in all_buttons(kwargs['reply_markup'])]
    assert telegram_bot.STATS_CALLBACK in [c.split('retry:', 1)[-1] for c in callbacks if c.startswith('retry:')]
    assert callbacks, 'B7: ≥1 действие-выход обязательно'


def test_stats_command_delivery_failure_replies_with_error(monkeypatch):
    """Сбой доставки сводки: ошибка с выходами, исключение не проброшено (B7)."""
    fake = FakeStatsManager(FULL_STATS)
    _install_stats(monkeypatch, fake)
    message = _FailFirstReplyMessage(chat=FakeChat())
    update = _command_update(message)

    run_coro(telegram_bot.handle_stats_command(update, None))  # не выбрасывает

    assert message.calls == 2  # сбой доставки + ответ об ошибке
    text, kwargs = message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_GENERIC
    callbacks = [b.callback_data for b in all_buttons(kwargs['reply_markup'])]
    assert f'retry:{telegram_bot.STATS_CALLBACK}' in callbacks


def test_stats_command_end_to_end_with_sqlite(sqlite_stats_app, monkeypatch):
    """Интеграция: реальная SQLite → менеджер → рендер → ответ команды."""
    db = db_module.db
    with sqlite_stats_app.app_context():
        db.session.add(UserStatistics(session_id='tg:777', date=date(2026, 9, 1), queries_count=5))
        db.session.add(MovieFeedback(user_id='777', kinopoisk_id=1, rating=8))
        db.session.add(Watchlist(user_id='777', kinopoisk_id=2, title='Фильм', year=2020, poster_url=None))
        db.session.commit()
    _install_stats(monkeypatch, FakeStatsManager(StatsManager(app=sqlite_stats_app).get_user_stats('777')))
    message = FakeMessage(chat=FakeChat())
    update = _command_update(message)

    run_coro(telegram_bot.handle_stats_command(update, None))

    text, kwargs = message.texts[0]
    assert kwargs['parse_mode'] == 'HTML'
    assert 'Запросов к боту: <b>5</b>' in text
    assert 'Оценок фильмов: <b>1</b> (средняя — <b>8.0</b>)' in text
    assert 'Фильмов в «Мой список»: <b>1</b>' in text


# === 5. Пункт меню `menu:stats` и регистрация (п.5.3) ===


def test_menu_stats_callback_shows_summary(monkeypatch):
    """menu:stats маршрутизируется диспетчером и показывает ту же сводку."""
    fake = FakeStatsManager(dict(FULL_STATS, queries=5))
    _install_stats(monkeypatch, fake)
    update = FakeCallbackUpdate('menu:stats', message=FakeMessage(), from_user=FakeUser(777))

    run_coro(telegram_bot.handle_movie_detail(update, None))

    assert fake.calls == ['777']
    assert update.callback_query.answer_calls == 1
    texts = update.callback_query.message.texts
    assert len(texts) == 1
    text, kwargs = texts[0]
    assert kwargs['parse_mode'] == 'HTML'
    assert 'Запросов к боту: <b>5</b>' in text


def test_menu_stats_callback_without_user_is_friendly(monkeypatch):
    """menu:stats без пользователя: guard до адаптера — объяснение, менеджер не вызван."""
    fake = FakeStatsManager()
    _install_stats(monkeypatch, fake)
    update = FakeCallbackUpdate('menu:stats', message=FakeMessage(), from_user=None)
    update.effective_user = None

    run_coro(telegram_bot.handle_movie_detail(update, None))

    assert fake.calls == []
    texts = update.callback_query.message.texts
    assert len(texts) == 1
    assert 'Не удалось определить пользователя' in texts[0][0]
    assert '/stats' in texts[0][0]  # подсказка команды — без dead-end


def test_main_menu_contains_stats_button():
    """Главное меню: кнопка «📊 Статистика» (menu:stats) рядом с «💡 Помощь»."""
    menu = telegram_bot.get_main_menu()
    buttons = {b.text: b.callback_data for b in all_buttons(menu)}
    assert telegram_bot.STATS_MENU_BUTTON_TEXT in buttons
    assert buttons[telegram_bot.STATS_MENU_BUTTON_TEXT] == 'menu:stats'
    for text, callback in buttons.items():
        assert len(text) <= 64
        assert len(callback.encode('utf-8')) <= 64


def test_bot_commands_include_stats():
    """/stats зарегистрирована в меню команд клиента (spec telegram-bot-commands)."""
    commands = {c.command: c.description for c in telegram_bot.build_bot_commands()}
    assert 'stats' in commands
    assert 'статистик' in commands['stats'].lower()
    assert len(commands['stats']) <= 256


def test_create_telegram_app_registers_stats_handler(monkeypatch):
    """CommandHandler("stats", …) добавлен в create_telegram_app (по списку handlers)."""
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '123456:test-token-c5')
    application = telegram_bot.create_telegram_app()

    registered = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if isinstance(handler, CommandHandler):
                registered.update(handler.commands)

    assert 'stats' in registered
