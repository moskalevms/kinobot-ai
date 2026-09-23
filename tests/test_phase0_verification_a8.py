"""Тесты A8: сквозные приёмочные проверки фазы 0 (verify-phase0-uiux-tests).

Без сети: проверяются КРИТЕРИИ ПРИЁМКИ фазы 0 целиком (а не точечное
поведение отдельных задач A1–A7, уже покрытое их тестами):
- деградация формата списка и карточки к прежнему поведению при отсутствии
  новых данных (country / kinopoisk_url / rating_source / rt_score);
- RT-бейдж «🍅 NN%» на обеих поверхностях (строка списка и вердикт
  карточки) при валидном rt_score и полное отсутствие его следов без него;
- сводные лимиты Bot API: отображение ≤5 фильмов, подписи кнопок ≤64 и
  callback_data ≤64 байт, caption карточки ≤1024 с балансировкой HTML;
- все четыре callback-маршрута (info:/alt:/similar:/back:) доставляют
  результат РЕДАКТИРОВАНИЕМ текстового сообщения — без reply/delete
  (мок PTB `AsyncMock(spec=Message)`, паттерны — `tests/conftest.py`).
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

import telegram_bot
from conftest import (
    LONG_DESCRIPTION,
    FakeCallbackUpdate,
    FakeChat,
    FakeMessage,
    FakeUser,
    all_buttons,
    assert_html_balanced,
    editable_message,
    install_callback_mocks,
    make_manager,
    make_movie,
    number_buttons,
)
from conftest import BROKEN_ENTITY_RE as _BROKEN_ENTITY_RE
from dialogue_manager import (
    BUTTON_TEXT_LIMIT,
    CARD_BACK_CALLBACK,
    CARD_QUOTE_CLOSE,
    CARD_QUOTE_OPEN,
    CARD_QUOTE_SEPARATOR,
    LIST_DISPLAY_LIMIT,
    MOVIE_CARD_CAPTION_LIMIT,
    SAVE_CALLBACK_PREFIX,
    build_movie_card,
    format_list_line,
    format_movie_card,
    truncate_button_text,
)

# Фильм «прежнего формата»: НЕТ новых полей фазы 0 — country, kinopoisk_url,
# rating_source, rt_score (критерий приёмки A8: поведение вырождается в прежнее)
_LEGACY_MOVIE = {
    'id': 435, 'title': 'Дюна', 'year': 2021,
    'genre': 'фантастика', 'rating': 7.8,
    'description': 'Короткое описание',
}


# --- 3.1 Деградация к прежнему поведению без новых данных ---


def test_list_line_without_new_fields_matches_legacy_format():
    """Без country/url/rating_source/rt_score строка списка — прежнего формата."""
    line = format_list_line(1, _LEGACY_MOVIE)

    # Точное сравнение с прежним форматом покрывает и отсутствие ссылки,
    # и отсутствие источника/бейджа/висящих разделителей (nit n6 ревью R1)
    assert line == '1. <b>Дюна</b> (2021) · фантастика · ⭐ 7.8'


def test_card_without_new_fields_matches_legacy_verdict():
    """Карточка без новых полей — прежний вердикт A7 без ссылки/бейджа."""
    card = format_movie_card(_LEGACY_MOVIE)

    verdict = '🎬 <strong>Дюна (2021) — фантастика с рейтингом 7.8.</strong>'
    assert card == (
        f'{verdict}{CARD_QUOTE_SEPARATOR}{CARD_QUOTE_OPEN}Короткое описание{CARD_QUOTE_CLOSE}'
    )
    assert_html_balanced(card)


def test_legacy_list_renders_without_new_field_traces():
    """Список из «прежних» фильмов: 13 → 5 строк, без следов новых полей."""
    dm = make_manager()
    movies = [dict(_LEGACY_MOVIE, id=i, title=f'Фильм {i}') for i in range(1, 14)]

    response, keyboard = dm.render_movie_list(movies, 'Заголовок')

    assert len(response.splitlines()) == LIST_DISPLAY_LIMIT + 1
    assert '🍅' not in response and '<a href' not in response
    assert 'None' not in response and '()' not in response
    # Ровно 5 номерных кнопок (ряды до навигационного), а не «не больше ряда»
    buttons = number_buttons(keyboard)
    assert len(buttons) == LIST_DISPLAY_LIMIT
    assert [b.callback_data for b in buttons] == [f'info:{i}' for i in range(1, 6)]


def test_legacy_movie_card_delivered_as_text_without_link_button():
    """Минимальный «прежний» фильм: карточка без постера — текст, без «🔗 Кинопоиск»."""
    movie = {
        'id': 7, 'title': 'Старый фильм', 'year': 2000,
        'genre': 'драма', 'rating': 7.0, 'description': 'Описание',
    }
    text, markup = build_movie_card(movie)

    assert '<a href' not in text and '🍅' not in text
    assert_html_balanced(text)
    # Клавиатура вырождается: без ссылки остаются «🎬 Похожие»,
    # «📌 Сохранить» (B4) и «⬅️ К списку»
    buttons = all_buttons(markup)
    assert [b.callback_data for b in buttons] == ['similar:7', f'{SAVE_CALLBACK_PREFIX}7', CARD_BACK_CALLBACK]
    assert all(b.url is None for b in buttons)

    message = FakeMessage(chat=FakeChat())
    asyncio.run(telegram_bot._send_result(message, {
        'response': text, 'reply_markup': markup, 'movie': movie,
    }))
    assert message.photos == []
    assert len(message.texts) == 1
    assert message.texts[0][0] == text
    assert message.texts[0][1]['reply_markup'] is markup


# --- 3.2 RT-бейдж без регрессий (обе поверхности) ---


def test_rt_badge_present_on_list_line_and_card_verdict():
    """Валидный rt_score=91 — бейдж «🍅 91%» и в списке, и в карточке."""
    movie = make_movie(rt_score=91)

    assert '🍅 91%' in format_list_line(1, movie)
    assert '🍅 91%' in format_movie_card(movie)


def test_zero_rt_score_is_valid_data():
    """rt_score=0 — «0% одобрения»: бейдж выводится на обеих поверхностях."""
    movie = make_movie(rt_score=0)

    assert '🍅 0%' in format_list_line(1, movie)
    assert '🍅 0%' in format_movie_card(movie)


@pytest.mark.parametrize('rt_score', [None, '', 'abc', 101, -1, 91.5, True])
def test_no_rt_badge_traces_without_valid_score(rt_score):
    """Невалидный rt_score: следов бейджа нет ни в списке, ни в карточке."""
    movie = make_movie(rt_score=rt_score)

    line = format_list_line(1, movie)
    card = format_movie_card(movie)

    assert '🍅' not in line
    assert '🍅' not in card
    # Разделитель бейджа не повис в конце строки
    assert not line.endswith(' ·')


# --- 3.3 Сводные лимиты Bot API (фаза 0) ---


def test_display_limit_and_keyboard_limits_of_list():
    """Список: 13 фильмов → 5 строк и 5 номерных кнопок; лимиты Bot API соблюдены.

    Сводный инвариант лимитов (mn4 ревью R1): подписи номерных кнопок —
    эмодзи-цифры, навигационной — константа, от данных фильма они не зависят,
    поэтому обрезка ДЛИННОЙ подписи проверяется явно через
    `truncate_button_text` (точечное покрытие — A3, tasks.md 1.2), а
    callback_data нагружен реально длинным id фильма.
    """
    dm = make_manager()
    movies = [make_movie(id=10**15 + i, title=f'Фильм {i}') for i in range(1, 14)]

    response, keyboard = dm.render_movie_list(movies, 'Заголовок')

    assert LIST_DISPLAY_LIMIT == 5
    assert len(response.splitlines()) == LIST_DISPLAY_LIMIT + 1
    assert len(number_buttons(keyboard)) == LIST_DISPLAY_LIMIT
    for row in keyboard.inline_keyboard:
        for button in row:
            assert len(button.text) <= BUTTON_TEXT_LIMIT == 64
            assert len(button.callback_data.encode('utf-8')) <= 64
    # Длинное название проходит через хелпер обрезки: ≤64 и с маркером «…»
    long_title = 'Очень длинное название фильма, которое превышает лимит подписи кнопки Telegram' * 3
    truncated = truncate_button_text(long_title)
    assert len(long_title) > BUTTON_TEXT_LIMIT
    assert len(truncated) <= BUTTON_TEXT_LIMIT
    assert truncated.endswith('…')


def test_caption_budget_and_keyboard_limits_of_card():
    """Карточка: caption ≤1024, HTML сбалансирован, кнопки в лимитах."""
    movie = make_movie(id=123456789, description=LONG_DESCRIPTION, rt_score=91)

    text, markup = build_movie_card(movie)

    assert len(LONG_DESCRIPTION) > 5000
    assert len(text) <= MOVIE_CARD_CAPTION_LIMIT == 1024
    assert_html_balanced(text)
    assert _BROKEN_ENTITY_RE.search(text) is None
    for button in all_buttons(markup):
        assert len(button.text) <= BUTTON_TEXT_LIMIT == 64
        if button.callback_data:
            assert len(button.callback_data.encode('utf-8')) <= 64


# --- 3.4 Матрица «edit вместо reply» для четырёх маршрутов ---


def _assert_no_new_message(message: AsyncMock) -> None:
    """Ни один reply/delete не вызван: в чате не появилось новых сообщений."""
    message.reply_text.assert_not_awaited()
    message.reply_photo.assert_not_awaited()
    message.delete.assert_not_awaited()


def test_matrix_info_route_edits_message(monkeypatch):
    """info: из текстового списка — ОДИН edit_media, без reply/delete."""
    dm = make_manager()
    install_callback_mocks(monkeypatch, dm)
    message = editable_message(photo=())
    update = FakeCallbackUpdate('info:447301', message=message, from_user=FakeUser(777))

    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    message.edit_media.assert_awaited_once()
    message.edit_text.assert_not_awaited()
    _assert_no_new_message(message)
    assert update.callback_query.answer_calls == 1


def test_matrix_alt_route_edits_message(monkeypatch):
    """alt: новый список редактирует прежний (текст→текст — edit_text)."""
    dm = make_manager()
    dm.process_message = AsyncMock(return_value={
        'response': '<strong>Вот другие варианты:</strong>\n',
        'reply_markup': None,
    })
    install_callback_mocks(monkeypatch, dm)
    message = editable_message(photo=())
    update = FakeCallbackUpdate('alt:list', message=message, from_user=FakeUser(777))

    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    message.edit_text.assert_awaited_once()
    message.edit_media.assert_not_awaited()
    _assert_no_new_message(message)
    assert update.callback_query.answer_calls == 1


def test_matrix_similar_route_edits_message(monkeypatch):
    """similar: список похожих из текстовой карточки — edit_text, без reply."""
    dm = make_manager()
    dm.session_manager.get_session('777').last_movies = [make_movie(id=435, title='Дюна')]
    dm.movie_agent.recommend_movies = AsyncMock(
        return_value=[make_movie(id=900, title='Похожий фильм')]
    )
    install_callback_mocks(monkeypatch, dm)
    message = editable_message(photo=())
    update = FakeCallbackUpdate('similar:435', message=message, from_user=FakeUser(777))

    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    message.edit_text.assert_awaited_once()
    assert 'Похожий фильм' in message.edit_text.await_args.args[0]
    message.edit_media.assert_not_awaited()
    _assert_no_new_message(message)
    assert update.callback_query.answer_calls == 1


def test_matrix_back_route_edits_message(monkeypatch):
    """back: возврат к списку из текстовой карточки — edit_text, без reply."""
    dm = make_manager()
    dm.session_manager.get_session('777').last_movies = [
        make_movie(id=i, title=f'Фильм {i}') for i in range(1, 4)
    ]
    install_callback_mocks(monkeypatch, dm)
    message = editable_message(photo=())
    update = FakeCallbackUpdate(CARD_BACK_CALLBACK, message=message, from_user=FakeUser(777))

    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    message.edit_text.assert_awaited_once()
    message.edit_media.assert_not_awaited()
    _assert_no_new_message(message)
    assert update.callback_query.answer_calls == 1
