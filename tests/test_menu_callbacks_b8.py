"""Тесты B8: reply-меню → inline-кнопки с префиксными callback.

Изменение add-inline-menu-callbacks. Покрывают:
- каждый маршрут `menu:*` и `top:*` вызывает ожидаемый эффект (переис-
  пользование существующих маршрутов: mood/random/help/watchlist, LLM-
  пайплайн для топов);
- неизвестный префикс/сегмент — дружелюбная ошибка с кнопками выхода (B7);
- в исходнике `src/telegram_bot.py` не осталось диспетчеризации по точному
  тексту кнопок (проверка через чтение исходника + AST);
- ручной ввод «топ фильмов» в любом регистре уходит в LLM-пайплайн как есть;
- билдеры меню: `InlineKeyboardMarkup`, уникальные `callback_data` ≤64 байта,
  подписи ≤64 символа, год == `config.CURRENT_YEAR`, эмодзи по гайдлайну B6;
- `/start` снимает залипшую reply-клавиатуру НЕВИДИМЫМ служебным сообщением
  (минимальная заглушка + `ReplyKeyboardRemove` + немедленное удаление, B2),
  сбой отправки или удаления не ломает приветствие;
- `retry:top:…` повторяет ТОТ ЖЕ топ (без коллизии с целью `retry:top` B7);
- защита делегирования (minor m1 ревью, design.md D7): сбой доставки
  `menu:help` — ошибка с кнопками (B7), `retry:menu:help` повторяет действие.

Все проверки офлайн: фейки PTB/aiohttp из tests/conftest.py, сеть и
реальный Telegram не используются.
"""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Tuple
from unittest.mock import AsyncMock

import pytest
from telegram import InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import NetworkError

import telegram_bot
from conftest import (
    FakeCallbackUpdate,
    FakeChat,
    FakeMessage,
    FakeSentMessage,
    FakeUser,
    all_buttons,
    editable_message,
    install_callback_mocks,
    make_card_result,
    make_manager,
)
from config import CURRENT_YEAR
from dialogue_manager import RANDOM_MOVIE_CALLBACK, WATCHLIST_PAGE_LIMIT

SRC_FILE = Path(__file__).resolve().parents[1] / 'src' / 'telegram_bot.py'


def _run(coro):
    return asyncio.run(coro)


def _dispatch(data: str, message: Any = None, from_user: Any = FakeUser(777), context: Any = None):
    """Прогнать callback через диспетчер и вернуть update (фейки conftest)."""
    update = FakeCallbackUpdate(data, message=message, from_user=from_user)
    _run(telegram_bot.handle_movie_detail(update, context))
    return update


def _callbacks(markup: Any) -> List[str]:
    return [b.callback_data for b in all_buttons(markup)]


class _TextMessage(FakeMessage):
    """Сообщение с текстом — для проверки handle_message."""

    def __init__(self, text: str):
        super().__init__()
        self.text = text


class _FakeWatchlist:
    """Минимальная заглушка WatchlistManager: одна страница списка."""

    def __init__(self, items: List[dict], total: int):
        self.items = items
        self.total = total
        self.list_calls: List[Tuple[Any, ...]] = []

    def list_page(self, user_id, offset, limit):
        self.list_calls.append((user_id, offset, limit))
        return self.items[offset:offset + limit], self.total


def _watchlist_items(count: int) -> List[dict]:
    return [
        {'kinopoisk_id': i, 'title': f'Фильм {i}', 'year': 2000 + i,
         'poster_url': None, 'added_at': None}
        for i in range(1, count + 1)
    ]


# === 1. Маршруты menu:* — ожидаемый эффект каждого ===


def test_menu_mood_sends_shared_prompt(monkeypatch):
    """menu:mood — тот же текст приглашения, что /mood и mood:start (DRY)."""
    # Мок менеджера диалога (fix-mood-offtopic-b1, D8): после доставки
    # приглашения маршрут пишет признак ожидания настроения в сессию —
    # in-memory заглушка исключает обращение к реальной PostgreSQL
    install_callback_mocks(monkeypatch, make_manager())
    update = _dispatch('menu:mood')

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._MOOD_PROMPT_HTML
    assert kwargs.get('parse_mode') == 'HTML'
    assert update.callback_query.answer_calls == 1


def test_menu_top_shows_top_submenu():
    """menu:top — подменю топов: заголовок + inline-кнопки top:*."""
    update = _dispatch('menu:top')

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._TOP_MENU_TITLE
    assert set(_callbacks(kwargs['reply_markup'])) == {
        'top:50:movies', 'top:50:series',
        f'top:movies:{CURRENT_YEAR}', f'top:series:{CURRENT_YEAR}',
        'top:menu',
    }


def test_menu_genre_sends_shared_genre_prompt():
    """menu:genre — то же приглашение жанра, что команда /genre."""
    update = _dispatch('menu:genre')
    menu_text = update.callback_query.message.texts[0][0]

    command_update = SimpleNamespace(message=FakeMessage())
    _run(telegram_bot.handle_genre_command(command_update, None))
    command_text = command_update.message.texts[0][0]

    assert menu_text == command_text == telegram_bot._GENRE_PROMPT_TEXT


def test_menu_random_delivers_card(monkeypatch):
    """menu:random — тот же маршрут случайного фильма, что random:movie."""
    dm = make_manager()
    dm.get_random_movie = AsyncMock(return_value=make_card_result())  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)
    message = editable_message(photo=())

    _dispatch('menu:random', message=message)

    # Карточка с постером доставлена редактированием сообщения (A5)
    message.edit_media.assert_awaited_once()
    dm.get_random_movie.assert_awaited_once()
    assert dm.get_random_movie.await_args.args[1] == '777'


def test_menu_watchlist_shows_first_page(monkeypatch):
    """menu:watchlist — тот же код, что /list (handle_watchlist_command)."""
    fake = _FakeWatchlist(_watchlist_items(1), 1)
    monkeypatch.setattr(telegram_bot, 'get_watchlist_manager', lambda: fake)

    update = _dispatch('menu:watchlist')

    assert fake.list_calls == [('777', 0, WATCHLIST_PAGE_LIMIT)]
    text = update.callback_query.message.texts[0][0]
    assert '1. <b>Фильм 1</b>' in text


def test_menu_help_sends_help_with_inline_menu():
    """menu:help — справка с inline-меню (тот же код, что /help)."""
    update = _dispatch('menu:help')

    text, kwargs = update.callback_query.message.texts[0]
    assert 'Примеры запросов' in text
    assert kwargs.get('parse_mode') == 'HTML'
    assert isinstance(kwargs.get('reply_markup'), InlineKeyboardMarkup)
    assert 'menu:mood' in _callbacks(kwargs['reply_markup'])


def test_menu_new_clears_session_and_greets(monkeypatch):
    """menu:new — очистка истории + in-memory контекста и приветствие с меню."""
    dm = make_manager()
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    # Предварительно создаём сессию пользователя — она должна исчезнуть
    dm.session_manager.get_session('777')  # type: ignore[attr-defined]
    context = SimpleNamespace(user_data={telegram_bot._LAST_QUERY_KEY: 'старый запрос'})

    update = _dispatch('menu:new', context=context)

    assert '777' not in dm.session_manager.sessions  # type: ignore[attr-defined]
    assert context.user_data == {}
    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._NEW_DIALOG_TEXT
    assert 'menu:mood' in _callbacks(kwargs['reply_markup'])


def test_menu_main_returns_main_menu():
    """menu:main и top:menu — возврат к главному меню (inline)."""
    for data in ('menu:main', 'top:menu'):
        update = _dispatch(data)
        text, kwargs = update.callback_query.message.texts[0]
        assert text == telegram_bot._BACK_TO_MAIN_TEXT
        assert isinstance(kwargs['reply_markup'], InlineKeyboardMarkup)
        assert 'menu:mood' in _callbacks(kwargs['reply_markup'])


# === 2. Маршруты top:* — LLM-пайплайн с прежними текстами запросов ===


@pytest.mark.parametrize('data,expected_query', [
    ('top:50:movies', 'топ 50 фильмов'),
    ('top:50:series', 'топ 50 сериалов'),
    (f'top:movies:{CURRENT_YEAR}', f'топ фильмов {CURRENT_YEAR}'),
    (f'top:series:{CURRENT_YEAR}', f'топ сериалов {CURRENT_YEAR}'),
])
def test_top_routes_run_llm_pipeline_with_same_query(monkeypatch, data, expected_query):
    """Кнопки топов запускают тот же запрос, что прежние reply-кнопки."""
    dm = make_manager()
    dm.process_message = AsyncMock(return_value={'response': 'ответ', 'reply_markup': None})  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)

    _dispatch(data)

    dm.process_message.assert_awaited_once()
    args = dm.process_message.await_args.args
    assert args[1] == '777'
    assert args[2] == expected_query


def test_top_route_tracks_client_request(monkeypatch):
    """Клик по топу учитывается в клиентской статистике (как у alt:)."""
    dm = make_manager()
    dm.process_message = AsyncMock(return_value={'response': 'ответ', 'reply_markup': None})  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)
    tracked: List[str] = []
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda key: tracked.append(key))

    _dispatch('top:50:movies')

    assert tracked == ['tg:777']


def test_top_route_failure_offers_retry_of_same_top(monkeypatch):
    """Сбой запроса топа — «🔄 Повторить» с ПОЛНЫМ callback_data (design D5)."""
    dm = make_manager()
    dm.process_message = AsyncMock(side_effect=NetworkError('нет связи'))  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)

    update = _dispatch(f'top:movies:{CURRENT_YEAR}')

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_NETWORK
    assert f'retry:top:movies:{CURRENT_YEAR}' in _callbacks(kwargs['reply_markup'])


def test_retry_top_segment_repeats_same_top(monkeypatch):
    """retry:top:50:movies повторяет «топ 50 фильмов», а НЕ «топ комедий» (B7)."""
    dm = make_manager()
    dm.process_message = AsyncMock(return_value={'response': 'ответ', 'reply_markup': None})  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)

    _dispatch('retry:top:50:movies')

    dm.process_message.assert_awaited_once()
    assert dm.process_message.await_args.args[2] == 'топ 50 фильмов'


def test_retry_top_exact_target_still_means_comedies(monkeypatch):
    """Регресс B7: exact-цель retry:top («топ комедий») не сломана веткой top:."""
    dm = make_manager()
    dm.process_message = AsyncMock(return_value={'response': 'ответ', 'reply_markup': None})  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)

    _dispatch('retry:top')

    assert dm.process_message.await_args.args[2] == telegram_bot._RETRY_TOP_QUERY


def test_top_route_without_user_is_friendly():
    """Пользователь не определён — служебный ответ с текстом запроса, без падения."""
    update = _dispatch('top:50:movies', from_user=None)

    text, kwargs = update.callback_query.message.texts[0]
    assert 'Не удалось определить пользователя' in text
    assert 'топ 50 фильмов' in text  # следующий шаг — ручной ввод, он сработает
    assert kwargs.get('parse_mode') == 'HTML'


# === 3. Неизвестные префикс/сегмент — дружелюбный выход (регресс B7) ===


@pytest.mark.parametrize('data', ['xyz:mood', 'menu:unknown', 'top:unknown', 'menu:', 'top:'])
def test_unknown_route_or_segment_is_friendly(data):
    """Неизвестный префикс/сегмент — объяснение с кнопками, без dead-end."""
    update = _dispatch(data)

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_UNKNOWN_CALLBACK
    callbacks = _callbacks(kwargs['reply_markup'])
    assert 'back:list' in callbacks
    assert RANDOM_MOVIE_CALLBACK in callbacks
    assert update.callback_query.answer_calls == 1


def test_menu_routes_without_user_are_friendly(monkeypatch):
    """menu:watchlist/menu:new без пользователя — служебный ответ, не AttributeError."""
    fake = _FakeWatchlist(_watchlist_items(1), 1)
    monkeypatch.setattr(telegram_bot, 'get_watchlist_manager', lambda: fake)

    for data in ('menu:watchlist', 'menu:new'):
        update = _dispatch(data, from_user=None, message=FakeMessage())
        text = update.callback_query.message.texts[0][0]
        assert 'Не удалось определить пользователя' in text
    assert fake.list_calls == []  # запрос в хранилище без пользователя не ушёл


# === 4. Исходник: диспетчеризации по точному тексту кнопок нет ===


_OLD_BUTTON_TEXTS = (
    '🎭 Фильм по настроению', '🏆 Топ фильмов', '⬅️ Назад', '🎬 Поиск по жанру',
    '🔄 Другие варианты', '💡 Помощь', '📌 Мой список', '🆕 Новый диалог',
    '🏆 Топ 50 фильмов', '📺 Топ 50 сериалов',
)


def test_source_has_no_button_text_dispatch():
    """В src/telegram_bot.py нет сравнений текста сообщения с подписями кнопок."""
    source = SRC_FILE.read_text(encoding='utf-8')
    # Прямой маркер прежнего паттерна
    assert 'user_message ==' not in source
    # AST: ни одного сравнения со строковым литералом прежних кнопок
    compared_literals = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Compare):
            for operand in (node.left, *node.comparators):
                if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
                    compared_literals.add(operand.value)
    for label in _OLD_BUTTON_TEXTS:
        assert label not in compared_literals, f'найдено сравнение с текстом кнопки: {label!r}'


def test_source_has_no_reply_keyboard():
    """Reply-клавиатуры удалены: нет ReplyKeyboardMarkup/KeyboardButton (design D1)."""
    source = SRC_FILE.read_text(encoding='utf-8')
    assert 'ReplyKeyboardMarkup' not in source
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == 'telegram':
            imported.update(alias.name for alias in node.names)
    assert 'KeyboardButton' not in imported
    assert 'ReplyKeyboardMarkup' not in imported
    assert 'ReplyKeyboardRemove' in imported  # снятие залипшей клавиатуры


# === 5. Ручной ввод уходит в LLM-пайплайн как есть ===


@pytest.mark.parametrize('text', [
    'топ фильмов', 'ТОП ФИЛЬМОВ', f'Топ Фильмов {CURRENT_YEAR}',
    '🏆 Топ фильмов', '🎭 Фильм по настроению',
])
def test_manual_text_goes_to_llm_pipeline(monkeypatch, text):
    """Любой ручной ввод (в т.ч. прежние подписи кнопок) — в _process_and_reply."""
    calls: List[Tuple[str, str]] = []

    async def _fake_process(update, context, user_id, query):
        calls.append((user_id, query))

    monkeypatch.setattr(telegram_bot, '_process_and_reply', _fake_process)
    message = _TextMessage(text)
    update = SimpleNamespace(message=message, effective_user=FakeUser(777))

    _run(telegram_bot.handle_message(update, None))

    assert calls == [('777', text)]
    assert message.texts == []  # меню-ответов на текст больше нет


def test_manual_top_text_end_to_end(monkeypatch):
    """«ТОП ФИЛЬМОВ» текстом обрабатывается LLM-пайплайном и не падает."""
    dm = make_manager()
    dm.process_message = AsyncMock(return_value={'response': 'Вот топ!', 'reply_markup': None})  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)
    message = _TextMessage('ТОП ФИЛЬМОВ')
    update = SimpleNamespace(message=message, effective_user=FakeUser(777))
    context = SimpleNamespace(user_data={})

    _run(telegram_bot.handle_message(update, context))

    dm.process_message.assert_awaited_once()
    assert dm.process_message.await_args.args[2] == 'ТОП ФИЛЬМОВ'
    assert message.texts[0][0] == 'Вот топ!'
    # Запрос сохранён для кнопки «🔄 Повторить» (B7)
    assert context.user_data[telegram_bot._LAST_QUERY_KEY] == 'ТОП ФИЛЬМОВ'


def test_empty_message_still_asks_for_query(monkeypatch):
    """Пустой после sanitize ввод — прежнее уточнение, LLM не вызывается."""
    async def _fail_process(*args, **kwargs):
        raise AssertionError('_process_and_reply не должен вызываться для пустого текста')

    monkeypatch.setattr(telegram_bot, '_process_and_reply', _fail_process)
    message = _TextMessage('')
    update = SimpleNamespace(message=message, effective_user=FakeUser(777))

    _run(telegram_bot.handle_message(update, None))

    assert message.texts[0][0] == 'Пожалуйста, введите запрос.'


# === 6. Билдеры меню: тип, лимиты, уникальность, год, эмодзи ===


def test_menu_builders_are_inline_with_valid_limits():
    """InlineKeyboardMarkup, уникальные callback ≤64 байт, подписи ≤64 символа."""
    main = telegram_bot.get_main_menu()
    top = telegram_bot.get_top_menu()
    assert isinstance(main, InlineKeyboardMarkup)
    assert isinstance(top, InlineKeyboardMarkup)

    buttons = all_buttons(main) + all_buttons(top)
    callbacks = [b.callback_data for b in buttons]
    assert len(callbacks) == len(set(callbacks)), 'callback_data кнопок меню не уникальны'
    for button in buttons:
        assert len(button.text) <= 64
        assert len(button.callback_data.encode('utf-8')) <= 64

    assert {b.callback_data for b in all_buttons(main)} == {
        'menu:mood', 'menu:top', 'menu:genre', 'menu:random',
        'menu:watchlist', 'menu:new', 'menu:stats', 'menu:help',
    }


def test_top_menu_year_comes_from_config():
    """Год в callback и подписях топов — config.CURRENT_YEAR (без хардкода)."""
    top = telegram_bot.get_top_menu()
    buttons = {b.callback_data: b.text for b in all_buttons(top)}
    assert f'top:movies:{CURRENT_YEAR}' in buttons
    assert f'top:series:{CURRENT_YEAR}' in buttons
    assert str(CURRENT_YEAR) in buttons[f'top:movies:{CURRENT_YEAR}']
    assert str(CURRENT_YEAR) in buttons[f'top:series:{CURRENT_YEAR}']
    # Ключи словаря запросов совпадают с сегментами кнопок
    assert set(telegram_bot._TOP_QUERY_BY_SEGMENT) == {
        '50:movies', '50:series', f'movies:{CURRENT_YEAR}', f'series:{CURRENT_YEAR}',
    }


def test_menu_buttons_follow_emoji_guideline():
    """Эмодзи кнопок меню — гайдлайн B6: ядро, ≤1 на кнопку, без запрещённых."""
    buttons = all_buttons(telegram_bot.get_main_menu()) + all_buttons(telegram_bot.get_top_menu())
    for button in buttons:
        for forbidden in ('😂', '🤣', '🙂', '👍', '💀', '🗿'):
            assert forbidden not in button.text
        emoji_count = sum(
            1 for ch in button.text
            if ord(ch) > 0x2000
            and not 0xFE00 <= ord(ch) <= 0xFE0F
            and ch not in '—«»…'
        )
        assert emoji_count <= 1


# === 7. /start снимает залипшую reply-клавиатуру (B2: send+delete) ===


def test_start_removal_stub_is_sent_and_deleted():
    """/start: приветствие ПЕРВОЕ; снятие клавиатуры — невидимой заглушкой (B2).

    Пересмотр способа доставки B8 design.md D4: служебное сообщение с
    минимальным текстом и `ReplyKeyboardRemove` отправляется и СРАЗУ
    удаляется — пользователь видит ровно одно сообщение (приветствие).
    """
    update = SimpleNamespace(message=FakeMessage(chat=FakeChat()))

    _run(telegram_bot.start(update, None))

    assert len(update.message.texts) == 2
    assert update.message.texts[0][0] == telegram_bot._WELCOME_HTML
    stub_text, stub_kwargs = update.message.texts[1]
    assert isinstance(stub_kwargs.get('reply_markup'), ReplyKeyboardRemove)
    assert stub_text == telegram_bot._KEYBOARD_REMOVAL_STUB_TEXT
    # Видимость: служебное сообщение удалено, приветствие осталось
    assert update.message.sent[1].deleted is True
    assert update.message.sent[0].deleted is False


def test_start_keyboard_removal_failure_is_silent():
    """Сбой ОТПРАВКИ снятия клавиатуры не ломает приветствие (fail-silent)."""

    class _FailRemovalMessage(FakeMessage):
        async def reply_text(self, text, **kwargs):
            if isinstance(kwargs.get('reply_markup'), ReplyKeyboardRemove):
                raise NetworkError('Telegram недоступен')
            return await super().reply_text(text, **kwargs)

    update = SimpleNamespace(message=_FailRemovalMessage(chat=FakeChat()))

    _run(telegram_bot.start(update, None))  # исключение не всплывает

    assert len(update.message.texts) == 1
    assert update.message.texts[0][0] == telegram_bot._WELCOME_HTML


def test_start_removal_stub_delete_failure_is_silent():
    """Сбой УДАЛЕНИЯ служебного сообщения не ломает приветствие (fail-silent, B2).

    Деградация косметическая: снятие клавиатуры уже доставлено, пользователь
    видит точку-заглушку вместо удалённого сообщения; предупреждение — в лог.
    """

    async def _fail_delete() -> None:
        raise NetworkError('Telegram недоступен')

    class _FailDeleteMessage(FakeMessage):
        async def reply_text(self, text, **kwargs):
            sent = await super().reply_text(text, **kwargs)
            if isinstance(kwargs.get('reply_markup'), ReplyKeyboardRemove):
                sent.delete = _fail_delete  # имитация отказа delete()
            return sent

    update = SimpleNamespace(message=_FailDeleteMessage(chat=FakeChat()))

    _run(telegram_bot.start(update, None))  # исключение не всплывает

    assert len(update.message.texts) == 2
    assert update.message.texts[0][0] == telegram_bot._WELCOME_HTML
    assert update.message.sent[1].deleted is False


# === 8. Защита делегирования menu:help (minor m1 ревью, design.md D7) ===


class _FailFirstReplyMessage(FakeMessage):
    """Сообщение, у которого ПЕРВЫЙ `reply_text` падает сетевой ошибкой.

    Последующие вызовы записываются штатно: ответ об ошибке (через
    `_callback_failure`) обязан дойти до пользователя — имитируется разовый
    сбой доставки, а не полностью недоступный Telegram.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.calls = 0

    async def reply_text(self, text: str, **kwargs: Any) -> FakeSentMessage:
        self.calls += 1
        if self.calls == 1:
            raise NetworkError('Telegram недоступен')
        return await super().reply_text(text, **kwargs)


def test_menu_help_delivery_failure_replies_with_actions():
    """menu:help: сбой доставки — ошибка с ≥1 кнопкой, исключение не проброшено.

    Регресс minor m1 ревью B8: `handle_help` не имеет внутреннего try/except —
    до защиты делегирования исключение уходило в глобальный error_handler,
    и пользователь не получал ни сообщения, ни кнопок (нарушение B7/D7).
    """
    message = _FailFirstReplyMessage()

    update = _dispatch('menu:help', message=message)  # не выбрасывает исключение

    assert message.calls == 2  # сбой доставки справки + ответ об ошибке
    text, kwargs = message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_NETWORK  # classify_error: сетевой класс
    buttons = _callbacks(kwargs['reply_markup'])
    assert buttons, 'Ответ об ошибке без кнопок — нарушение B7 «≥1 действие»'
    assert 'retry:menu:help' in buttons  # повтор ТОГО ЖЕ действия меню (D7)
    assert update.callback_query.answer_calls == 1  # answer() не дублируется


def test_retry_menu_help_repeats_same_action():
    """retry:menu:help — кнопка «🔄 Повторить» действительно открывает справку."""
    update = _dispatch('retry:menu:help')

    text, kwargs = update.callback_query.message.texts[0]
    assert 'Примеры запросов' in text
    assert kwargs.get('parse_mode') == 'HTML'
    assert 'menu:mood' in _callbacks(kwargs['reply_markup'])
