"""Тесты B1: онбординг «ценность сразу» (add-onboarding-and-error-recovery).

Без сети и реального Telegram: /start — одно короткое сообщение с
inline-кнопками мгновенных действий; маршрут `random:movie` доставляет
карточку случайного фильма (edit-путь A5 и fallback новым сообщением);
пустой результат/сбой — дружелюбная ошибка класса «сеть/Kinopoisk» с
кнопкой повтора (B7); `mood:start` — единый текст приглашения.
Мок-паттерны — общие фейки из `tests/conftest.py`.
"""
import asyncio
from types import SimpleNamespace
from typing import Any, List, Tuple
from unittest.mock import AsyncMock

import telegram_bot
from conftest import (
    FakeCallbackUpdate,
    FakeChat,
    FakeHttpSession,
    FakeMessage,
    FakeUser,
    all_buttons,
    assert_html_balanced,
    editable_message,
    install_callback_mocks,
    make_card_result,
    make_manager,
    make_movie,
)
from dialogue_manager import RANDOM_CANDIDATE_LIMIT, RANDOM_MIN_RATING
from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup


def _run(coro):
    return asyncio.run(coro)


class _StartUpdate:
    """Update для команды /start: только `.message` (фейк из conftest)."""

    def __init__(self):
        self.message = FakeMessage(chat=FakeChat())


# --- 1. /start: одно короткое сообщение + inline-кнопки ---


def test_start_sends_single_short_html_message():
    """/start — РОВНО одно сообщение ≤4 строк, parse_mode='HTML', без Markdown."""
    update = _StartUpdate()

    _run(telegram_bot.start(update, None))

    assert len(update.message.texts) == 1
    text, kwargs = update.message.texts[0]
    assert text == telegram_bot._WELCOME_HTML
    assert len(text.splitlines()) <= 4
    assert kwargs.get('parse_mode') == 'HTML'
    assert '*' not in text  # никакого Markdown
    assert_html_balanced(text)


def test_start_keyboard_has_both_instant_action_buttons():
    """Inline-кнопки: «🎲 Случайный фильм вечера» и «🎭 Подобрать по настроению»."""
    update = _StartUpdate()

    _run(telegram_bot.start(update, None))

    _, kwargs = update.message.texts[0]
    markup = kwargs.get('reply_markup')
    assert isinstance(markup, InlineKeyboardMarkup)
    buttons = all_buttons(markup)
    assert [(b.text, b.callback_data) for b in buttons] == [
        (telegram_bot.RANDOM_MOVIE_BUTTON_TEXT, telegram_bot.RANDOM_MOVIE_CALLBACK),
        (telegram_bot.MOOD_BUTTON_TEXT, telegram_bot.MOOD_START_CALLBACK),
    ]
    for button in buttons:
        # Лимиты Bot API: подпись ≤64 символа, callback_data ≤64 байта
        assert len(button.text) <= 64
        assert len(button.callback_data.encode('utf-8')) <= 64


def test_start_does_not_attach_reply_menu():
    """Одно сообщение не несёт два reply_markup: reply-меню к /start не крепится."""
    update = _StartUpdate()

    _run(telegram_bot.start(update, None))

    _, kwargs = update.message.texts[0]
    assert not isinstance(kwargs.get('reply_markup'), ReplyKeyboardMarkup)


def test_welcome_follows_emoji_guideline():
    """Эмодзи приветствия — только из гайдлайна B6 (избегаемые запрещены)."""
    for forbidden in ('😂', '🤣', '🙂', '👍', '💀', '🗿'):
        assert forbidden not in telegram_bot._WELCOME_HTML
    # Не более одного эмодзи на строку (🎬 и ⚠️ — ядро гайдлайна);
    # вариационные селекторы (U+FE0F) не считаются отдельным символом
    for line in telegram_bot._WELCOME_HTML.splitlines():
        emoji_count = sum(
            1 for ch in line
            if ord(ch) > 0x2000
            and not 0xFE00 <= ord(ch) <= 0xFE0F
            and ch not in '—«»…'
        )
        assert emoji_count <= 1


def test_main_menu_and_text_dispatch_preserved_until_b8():
    """Reply-меню и диспетчеризация по тексту кнопок сохранены (до B8)."""
    menu = telegram_bot.get_main_menu()
    assert isinstance(menu, ReplyKeyboardMarkup)
    menu_texts = {b.text for row in menu.keyboard for b in row}
    assert '🎭 Фильм по настроению' in menu_texts
    assert '🆕 Новый диалог' in menu_texts

    # Текстовая диспетчеризация работает: «⬅️ Назад» возвращает главное меню
    class _TextMessage(FakeMessage):
        def __init__(self, text: str):
            super().__init__()
            self.text = text

    update = SimpleNamespace(message=_TextMessage('⬅️ Назад'), effective_user=FakeUser(777))
    _run(telegram_bot.handle_message(update, None))
    text, kwargs = update.message.texts[0]
    assert 'Вернулись в главное меню' in text
    assert isinstance(kwargs.get('reply_markup'), ReplyKeyboardMarkup)


# --- 2. Маршрут random:movie ---


def _random_update(message: Any = None) -> FakeCallbackUpdate:
    return FakeCallbackUpdate(
        telegram_bot.RANDOM_MOVIE_CALLBACK,
        message=message if message is not None else FakeMessage(chat=FakeChat()),
        from_user=FakeUser(777),
    )


def test_random_callback_edits_message_into_card(monkeypatch):
    """Штатный путь (A5): карточка случайного фильма ЗАМЕНЯЕТ сообщение."""
    dm = make_manager()
    card = make_card_result()
    dm.get_random_movie = AsyncMock(return_value=card)  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)

    message = editable_message(photo=())
    update = _random_update(message)
    _run(telegram_bot.handle_movie_detail(update, None))

    message.edit_media.assert_awaited_once()
    media = message.edit_media.await_args.kwargs['media']
    assert media.caption == card['response']
    assert media.parse_mode == 'HTML'
    assert update.callback_query.answer_calls == 1


def test_random_callback_fallback_new_message(monkeypatch):
    """Fallback: нередактируемое сообщение — карточка новым сообщением (A4)."""
    dm = make_manager()
    dm.get_random_movie = AsyncMock(return_value=make_card_result())  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)

    update = _random_update()  # FakeMessage без edit-методов
    _run(telegram_bot.handle_movie_detail(update, None))

    photos: List[Tuple[Any, Any]] = update.callback_query.message.photos
    assert len(photos) == 1
    assert photos[0][1].get('parse_mode') == 'HTML'


def test_random_callback_empty_result_is_network_error_with_retry(monkeypatch):
    """Пустой результат движка — класс «сеть/Kinopoisk» + «🔄 Повторить» (B7)."""
    dm = make_manager()
    dm.get_random_movie = AsyncMock(return_value=None)  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)

    update = _random_update()
    _run(telegram_bot.handle_movie_detail(update, None))

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_NETWORK
    assert kwargs.get('parse_mode') == 'HTML'
    callbacks = [b.callback_data for b in all_buttons(kwargs['reply_markup'])]
    assert 'retry:random' in callbacks
    # Дубль действия исключён (гайдлайн B6: 1 эмодзи = 1 смысл): «🎲 Случайный
    # фильм» делал бы то же, что «🔄 Повторить», поэтому вторым выходом идёт
    # «🏆 Топ комедий» — два РАЗНЫХ действия, ≥1 действие сохранено
    assert 'retry:top' in callbacks
    assert telegram_bot.RANDOM_MOVIE_CALLBACK not in callbacks


def test_random_callback_tracks_client_request(monkeypatch):
    """MUST спеки bot-onboarding: клик «🎲 Случайный фильм вечера» учтён в статистике."""
    dm = make_manager()
    dm.get_random_movie = AsyncMock(return_value=make_card_result())  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)
    tracked: List[str] = []
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda sid, *a, **kw: tracked.append(sid))

    update = _random_update()
    _run(telegram_bot.handle_movie_detail(update, None))

    # Образец — test_compact_buttons_a3.py: идентификатор «tg:{user_id}»
    assert tracked == ['tg:777']


def test_random_callback_exception_is_classified(monkeypatch):
    """Исключение маршрута — классифицированная ошибка с повтором, лог сохранён."""
    import aiohttp

    dm = make_manager()
    dm.get_random_movie = AsyncMock(side_effect=aiohttp.ClientError('API недоступен'))  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)

    update = _random_update()
    _run(telegram_bot.handle_movie_detail(update, None))  # не должно бросить

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_NETWORK
    callbacks = [b.callback_data for b in all_buttons(kwargs['reply_markup'])]
    assert 'retry:random' in callbacks


def test_random_callback_without_user_is_friendly(monkeypatch):
    """Пользователя не определить — дружелюбный выход, не падение."""
    dm = make_manager()
    dm.get_random_movie = AsyncMock()  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)

    update = FakeCallbackUpdate(telegram_bot.RANDOM_MOVIE_CALLBACK, from_user=None)
    _run(telegram_bot.handle_movie_detail(update, None))

    assert 'Не удалось определить пользователя' in update.callback_query.message.texts[0][0]
    dm.get_random_movie.assert_not_awaited()


# --- 3. Маршрут mood:start ---


def test_mood_callback_sends_shared_prompt_as_new_message():
    """«🎭 Подобрать по настроению» — единый текст `_MOOD_PROMPT_HTML` новым сообщением."""
    update = FakeCallbackUpdate(telegram_bot.MOOD_START_CALLBACK, from_user=FakeUser(777))

    _run(telegram_bot.handle_movie_detail(update, None))

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._MOOD_PROMPT_HTML
    assert kwargs.get('parse_mode') == 'HTML'
    assert update.callback_query.answer_calls == 1


# --- 4. DialogueManager.get_random_movie (без сети) ---


def test_get_random_movie_reuses_engine_and_saves_session():
    """Один вызов движка с критериями B1; фильм сохраняется в сессию."""
    dm = make_manager()
    movies = [make_movie(id=1, title='Первый'), make_movie(id=2, title='Второй')]
    dm.movie_agent.recommend_movies = AsyncMock(return_value=movies)  # type: ignore[method-assign]
    # MUST спеки bot-onboarding: маршрут детерминированный — LLM-классификатор
    # интентов не вызывается (иначе «ценность сразу» теряет секунды)
    dm.intent_classifier.classify_with_llm = AsyncMock()

    result = _run(dm.get_random_movie(FakeHttpSession(), '777'))

    dm.intent_classifier.classify_with_llm.assert_not_awaited()

    kwargs = dm.movie_agent.recommend_movies.await_args.kwargs
    assert kwargs['min_imdb_rating'] == RANDOM_MIN_RATING
    assert kwargs['limit'] == RANDOM_CANDIDATE_LIMIT
    assert kwargs['movie_type'] == 'movie'

    assert result is not None
    assert result['movie'] in movies
    assert result['movies_list'] == [result['movie']]
    assert isinstance(result['reply_markup'], InlineKeyboardMarkup)
    assert result['response']  # карточка собрана единым строителем
    # Сессия обновлена: кнопки карточки «Похожие»/«К списку» будут работать
    session = dm.session_manager.get_session('777')
    assert session.last_movies == [result['movie']]
    assert dm.session_manager.saved == ['777']


def test_get_random_movie_excludes_shown_movie():
    """Повторный тап не показывает тот же фильм, пока кандидатов ≥2."""
    dm = make_manager()
    shown = make_movie(id=1, title='Уже показан')
    fresh = make_movie(id=2, title='Свежий')
    dm.session_manager.get_session('777').last_movies = [shown]
    dm.movie_agent.recommend_movies = AsyncMock(return_value=[shown, fresh])  # type: ignore[method-assign]

    result = _run(dm.get_random_movie(FakeHttpSession(), '777'))

    assert result['movie'] == fresh


def test_get_random_movie_falls_back_to_full_list_when_all_shown():
    """Все кандидаты показаны — берём полный список (не тупик)."""
    dm = make_manager()
    shown = make_movie(id=1)
    dm.session_manager.get_session('777').last_movies = [shown]
    dm.movie_agent.recommend_movies = AsyncMock(return_value=[shown])  # type: ignore[method-assign]

    result = _run(dm.get_random_movie(FakeHttpSession(), '777'))

    assert result is not None
    assert result['movie'] == shown


def test_get_random_movie_returns_none_on_empty_candidates():
    """Пустые кандидаты (движок проглотил сбой API) — None для ошибки B7."""
    dm = make_manager()
    dm.movie_agent.recommend_movies = AsyncMock(return_value=[])  # type: ignore[method-assign]

    result = _run(dm.get_random_movie(FakeHttpSession(), '777'))

    assert result is None
    assert dm.session_manager.saved == []
