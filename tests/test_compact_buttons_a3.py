"""Тесты A3: компактные inline-кнопки списка (add-compact-buttons-and-movie-card).

Без сети: проверяются хелпер обрезки подписи кнопок (лимит Telegram 64),
раскладка клавиатуры списка (номерные кнопки ≤4 в ряд + отдельный ряд
навигации), соответствие кнопки `info:{id}` i-му фильму и маршрут `alt:`
(кнопка «🔄 Другие» ряда навигации — с B2 их три — запускает existing
alternative-пайплайн).
Мок-паттерны — общие фейки из `tests/conftest.py` (nit n4, изменение
verify-phase0-uiux-tests).
"""
import asyncio
import logging
from typing import List
from unittest.mock import AsyncMock

import telegram_bot
from conftest import (
    FakeCallbackUpdate,
    FakeChat,
    FakeHttpSession,
    FakeMessage,
    FakeUser,
    all_buttons as _all_buttons,
    make_manager as _manager,
    make_movie,
    number_buttons as _number_buttons,
)
from dialogue_manager import (
    BUTTONS_PER_ROW,
    BUTTON_TEXT_LIMIT,
    LIST_MORE_BUTTON_TEXT,
    LIST_NAV_BUTTON_TEXT,
    LIST_NAV_CALLBACK,
    LIST_RANDOM_BUTTON_TEXT,
    NUMBER_BUTTON_LABELS,
    PAGE_CALLBACK_PREFIX,
    RANDOM_MOVIE_CALLBACK,
    number_button_label,
    truncate_button_text,
)


def _movie(**overrides) -> dict:
    """Фильм A3: список не использует description/poster_url — убираем их.

    Обёртка над общей фабрикой `make_movie` (design.md D2): набор полей
    участвует в побайтовых assert'ах формата списка и не должен меняться.
    Ключи выкидываются ДО применения overrides (nit n5 ревью R1): явная
    передача description/poster_url в тесте не игнорируется.
    """
    movie = make_movie()
    movie.pop('description', None)
    movie.pop('poster_url', None)
    movie.update(overrides)
    return movie


# --- 5.1 Хелпер обрезки подписи кнопок ---


def test_long_text_truncated_to_limit_with_ellipsis():
    # Без пробелов: срез точно равен лимиту (висящий пробел перед «…» снимается)
    text = 'НазваниеФильмаКотороеЗаметноДлиннееЛимитаПодписиКнопкиTelegram' * 3
    assert len(text) > BUTTON_TEXT_LIMIT

    result = truncate_button_text(text)

    assert len(result) == BUTTON_TEXT_LIMIT
    assert result.endswith('…')
    # Начало исходного текста сохранено — пользователь видит, о чём кнопка
    assert result[:-1] == text[:BUTTON_TEXT_LIMIT - 1]


def test_truncation_never_exceeds_limit_with_spaces():
    """Реалистичное название с пробелами: лимит не превышен, «…» на месте."""
    text = 'Название фильма, которое заметно длиннее лимита подписи кнопки Telegram' * 3

    result = truncate_button_text(text)

    assert len(result) <= BUTTON_TEXT_LIMIT
    assert result.endswith('…')
    # Пробел перед «…» не остаётся
    assert not result.endswith(' …')


def test_none_empty_and_whitespace_give_placeholder():
    """Пустая подпись недопустима: None/''/пробелы → «—» (nit №1 ревью A2)."""
    for value in (None, '', '   ', '\t\n'):
        assert truncate_button_text(value) == '—'


def test_short_text_is_unchanged():
    for value in ('1️⃣', LIST_NAV_BUTTON_TEXT, 'Короткое название'):
        assert truncate_button_text(value) == value
        assert '…' not in truncate_button_text(value)


def test_exact_limit_is_not_truncated():
    text = 'ы' * BUTTON_TEXT_LIMIT
    assert truncate_button_text(text) == text


def test_custom_limit_is_respected():
    assert truncate_button_text('абвгдежз', limit=5) == 'абвг…'
    assert len(truncate_button_text('абвгдежз', limit=5)) == 5


def test_surrounding_whitespace_is_stripped():
    assert truncate_button_text('  Дюна  ') == 'Дюна'


def test_number_labels_fit_the_limit():
    """Эмодзи-цифры проходят как есть: len() по code point'ам достаточен."""
    for index in range(1, len(NUMBER_BUTTON_LABELS) + 1):
        label = number_button_label(index)
        assert label == NUMBER_BUTTON_LABELS[index - 1]
        assert truncate_button_text(label) == label
        assert len(label) <= BUTTON_TEXT_LIMIT


def test_number_label_fallback_for_index_above_ten():
    """Номер больше 10 — обычная десятичная запись (задел на рост лимита)."""
    assert number_button_label(11) == '11'
    assert number_button_label(137) == '137'
    # Вне диапазона 1..10 подпись не бывает пустой — кнопка остаётся валидной
    for index in (0, -1, 11):
        assert truncate_button_text(number_button_label(index))


# --- 5.2 Раскладка клавиатуры списка ---


def test_five_movies_give_two_number_rows_plus_nav_row():
    """Критерий A3 «≤2 рядов»: 2 ряда НОМЕРОВ при 5 фильмах + ряд навигации."""
    dm = _manager()
    movies = [_movie(id=i, title=f'Фильм {i}') for i in range(1, 14)]

    response, keyboard = dm._generate_list_response(movies, 'Заголовок')

    assert BUTTONS_PER_ROW == 4
    # Формат текста A2 не изменился: заголовок + 5 строк фильмов
    assert len(response.splitlines()) == 6
    rows = keyboard.inline_keyboard
    assert len(rows[: -1]) == 2
    assert [len(row) for row in rows[: -1]] == [4, 1]
    # Ряд навигации — отдельный, под номерами (B2: три контекстных quick
    # replies — другие варианты / следующая страница / случайный фильм)
    assert [b.text for b in rows[-1]] == [
        LIST_NAV_BUTTON_TEXT, LIST_MORE_BUTTON_TEXT, LIST_RANDOM_BUTTON_TEXT,
    ]
    assert rows[-1][0].callback_data == LIST_NAV_CALLBACK
    assert rows[-1][1].callback_data.startswith(PAGE_CALLBACK_PREFIX)
    assert rows[-1][2].callback_data == RANDOM_MOVIE_CALLBACK


def test_number_buttons_carry_ids_of_matching_movies():
    """Тап по 3️⃣ открывает карточку ТРЕТЬЕГО фильма списка."""
    dm = _manager()
    movies = [_movie(id=100 + i, title=f'Фильм {i}') for i in range(1, 6)]

    _, keyboard = dm._generate_list_response(movies, 'Заголовок')

    buttons = _number_buttons(keyboard)
    assert [b.text for b in buttons] == ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣']
    assert [b.callback_data for b in buttons] == ['info:101', 'info:102', 'info:103', 'info:104', 'info:105']
    # Кнопка с номером 3 → id третьего фильма
    assert buttons[2].callback_data == f'info:{movies[2]["id"]}'


def test_every_button_text_fits_telegram_limit():
    """Все подписи и callback_data в пределах лимитов Bot API."""
    dm = _manager()
    long_title = 'Очень длинное название фильма' * 10
    movies = [_movie(id=i, title=long_title) for i in range(1, 6)]

    _, keyboard = dm._generate_list_response(movies, 'Заголовок')

    for row in keyboard.inline_keyboard:
        for button in row:
            assert len(button.text) <= BUTTON_TEXT_LIMIT
            assert len(button.callback_data.encode('utf-8')) <= 64


def test_long_or_missing_title_does_not_break_keyboard():
    """Название не попадает в подпись: ни «None», ни превышения лимита."""
    dm = _manager()
    movies = [
        _movie(id=1, title=None),
        _movie(id=2, title=''),
        _movie(id=3, title='Название длиннее шестидесяти четырёх символов ' * 3),
    ]

    _, keyboard = dm._generate_list_response(movies, 'Заголовок')

    labels = [b.text for b in _number_buttons(keyboard)]
    assert labels == ['1️⃣', '2️⃣', '3️⃣']
    assert 'None' not in ''.join(labels)
    assert all(len(label) <= BUTTON_TEXT_LIMIT for label in labels)


def test_few_movies_single_number_row():
    """Лимит — верхняя граница: 2 фильма → один ряд номеров + навигация."""
    dm = _manager()
    movies = [_movie(id=i, title=f'Фильм {i}') for i in range(1, 3)]

    response, keyboard = dm._generate_list_response(movies, 'Заголовок')

    assert len(response.splitlines()) == 3
    assert len(keyboard.inline_keyboard[0]) == 2
    assert len(keyboard.inline_keyboard) == 2


def test_badge_and_title_are_not_in_buttons():
    """RT-бейдж и название остаются в тексте списка, а не в кнопках."""
    dm = _manager()
    movies = [_movie(id=447301, rt_score=91)]

    _, keyboard = dm._generate_list_response(movies, 'Заголовок')

    all_texts = ''.join(b.text for row in keyboard.inline_keyboard for b in row)
    assert '🍅' not in all_texts
    assert 'Дюна' not in all_texts
    assert keyboard.inline_keyboard[0][0].callback_data == 'info:447301'


def test_render_movie_list_is_a_public_delegate():
    """Публичный рендер (для «⬅️ К списку») даёт тот же результат."""
    dm = _manager()
    movies = [_movie(id=i, title=f'Фильм {i}') for i in range(1, 4)]

    assert dm.render_movie_list(movies, 'Заголовок')[0] == dm._generate_list_response(movies, 'Заголовок')[0]
    assert (
        dm.render_movie_list(movies, 'Заголовок')[1].to_dict()
        == dm._generate_list_response(movies, 'Заголовок')[1].to_dict()
    )


# --- 5.3 Маршрут alt: ---


def test_alt_callback_runs_alternative_pipeline(monkeypatch):
    """Кнопка под списком запускает тот же пайплайн, что reply-кнопка."""
    dm = _manager()
    dm.process_message = AsyncMock(return_value={
        'response': '<strong>Вот другие варианты:</strong>\n',
        'reply_markup': None,
    })
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    monkeypatch.setattr(telegram_bot.aiohttp, 'ClientSession', lambda *a, **kw: FakeHttpSession())
    tracked: List[str] = []
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda sid, *a, **kw: tracked.append(sid))

    message = FakeMessage(chat=FakeChat())
    update = FakeCallbackUpdate(LIST_NAV_CALLBACK, message=message, from_user=FakeUser(777))
    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    # Запрос и пользователь — те же, что у reply-кнопки «🔄 Другие варианты»
    assert dm.process_message.await_args.args[1:] == ('777', telegram_bot.OTHER_VARIANTS_QUERY)
    assert telegram_bot.OTHER_VARIANTS_QUERY == 'посоветуй другие фильмы'
    # Клик учтён в клиентской статистике (метрика активности по кнопкам)
    assert tracked == ['tg:777']
    # Ответ доставлен общим отправителем в то же сообщение
    assert message.texts[0][0] == '<strong>Вот другие варианты:</strong>\n'
    assert message.texts[0][1].get('parse_mode') == 'HTML'
    assert message.chat.actions == [{'action': 'typing'}]
    assert update.callback_query.answer_calls == 1


def test_alt_callback_without_user_is_friendly(monkeypatch):
    """Без user_id — дружелюбный ответ, а не исключение (без dead-end)."""
    dm = _manager()
    dm.process_message = AsyncMock()
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    monkeypatch.setattr(telegram_bot.aiohttp, 'ClientSession', lambda *a, **kw: FakeHttpSession())

    update = FakeCallbackUpdate(LIST_NAV_CALLBACK, from_user=None)
    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    dm.process_message.assert_not_awaited()
    text, _ = update.callback_query.message.texts[0]
    assert 'Не удалось определить пользователя' in text


def test_alt_callback_pipeline_error_does_not_crash(monkeypatch, caplog):
    """Сбой пайплайна — понятный ответ пользователю и запись в лог."""
    dm = _manager()
    dm.process_message = AsyncMock(side_effect=RuntimeError('сеть недоступна'))
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    monkeypatch.setattr(telegram_bot.aiohttp, 'ClientSession', lambda *a, **kw: FakeHttpSession())
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda *a, **kw: None)

    update = FakeCallbackUpdate(LIST_NAV_CALLBACK, from_user=FakeUser(777))
    with caplog.at_level(logging.ERROR):
        asyncio.run(telegram_bot.handle_movie_detail(update, None))

    # B7: сбой без маркеров сети/LLM — резервный класс с кнопками выхода,
    # «🔄 Повторить» повторяет ту же операцию (retry:alt)
    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_GENERIC
    assert kwargs.get('parse_mode') == 'HTML'
    callbacks = [b.callback_data for b in _all_buttons(kwargs['reply_markup'])]
    assert 'retry:alt' in callbacks
    assert telegram_bot.RANDOM_MOVIE_CALLBACK in callbacks
    # Логирование сохранено в прежнем объёме
    assert any('сеть недоступна' in r.message for r in caplog.records)
