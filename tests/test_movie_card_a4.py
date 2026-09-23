"""Тесты A4: карточка фильма одним сообщением (add-compact-buttons-and-movie-card).

Без сети: проверяются единая сборка карточки (текст + клавиатура), бюджет
caption ≤1024 символа с обрезкой описания по безопасной HTML-границе,
доставка ОДНИМ сообщением с постером и текстовый fallback без падения,
состав inline-клавиатуры карточки и маршруты callback `similar:` / `back:`.
Мок-паттерны — общие фейки из `tests/conftest.py` (nit n4, изменение
verify-phase0-uiux-tests).
"""
import asyncio
import logging
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock

import movie_agent
import telegram_bot
from conftest import (
    LONG_DESCRIPTION,
    FakeCallbackUpdate,
    FakeChat,
    FakeHttpSession,
    FakeMessage,
    FakeUser,
    all_buttons as _all_buttons,
    assert_html_balanced,
    install_callback_mocks as _install_callback_mocks,
    make_manager as _manager,
    make_movie as _movie,
)
from conftest import BROKEN_ENTITY_RE as _BROKEN_ENTITY_RE
from conftest import KP_PAYLOAD as _KP_PAYLOAD
from dialogue_manager import (
    CARD_BACK_BUTTON_TEXT,
    CARD_BACK_CALLBACK,
    CARD_LINK_BUTTON_TEXT,
    CARD_SAVE_BUTTON_TEXT,
    CARD_SIMILAR_BUTTON_TEXT,
    MOVIE_CARD_CAPTION_LIMIT,
    SAVE_CALLBACK_PREFIX,
    _cut_html_safely,
    _fit_card_head,
    build_movie_card,
    build_movie_card_keyboard,
    format_movie_card,
)

# Тот же фильм в виде словаря финальной выдачи (путь info-интента):
# оба рендерера обязаны дать одинаковый текст карточки
_INFO_MOVIE = {
    'id': 447301, 'title': 'Начало', 'year': 2010,
    'genre': 'фантастика', 'country': 'США',
    'rating': 8.8, 'rating_imdb': 8.8, 'rating_kp': 8.7,
    'description': 'Сон внутри сна',
    'poster_url': 'https://example.com/poster.jpg',
    'kinopoisk_url': 'https://www.kinopoisk.ru/film/447301/',
    'imdb_id': 'tt1375666',
    'rt_score': 91, 'metascore': 82,
}


# --- 6.2 Бюджет caption и безопасная обрезка ---


def test_long_description_fits_caption_budget():
    assert len(LONG_DESCRIPTION) > 5000

    text, _ = build_movie_card(_movie(description=LONG_DESCRIPTION, rt_score=91))

    assert len(text) <= MOVIE_CARD_CAPTION_LIMIT == 1024
    assert_html_balanced(text)
    # Обрезка обозначена внутри закрытой цитаты (A7), вердикт и RT-бейдж сохранены
    assert text.endswith('…</blockquote>')
    assert '🍅 91%' in text
    assert '<strong>Дюна (2021)' in text


def test_no_broken_html_entities_after_truncation():
    text, _ = build_movie_card(_movie(description=LONG_DESCRIPTION))

    # Каждый «&» начинает корректную сущность — обрывков «&am…» нет
    assert _BROKEN_ENTITY_RE.search(text) is None
    # HTML-символы описания экранированы: инъекция тегов невозможна
    assert '<b>важно</b>' not in text
    assert '&lt;b&gt;важно&lt;/b&gt;' in text


def test_short_description_card_is_byte_identical_to_formatter():
    """Обратная совместимость: в бюджете текст равен format_movie_card."""
    movie = _movie(rt_score=91)

    text, _ = build_movie_card(movie)

    assert text == format_movie_card(movie)


def test_missing_description_is_safe():
    text, _ = build_movie_card({'id': 1, 'title': 'Минимум'})

    assert len(text) <= MOVIE_CARD_CAPTION_LIMIT
    assert_html_balanced(text)
    assert 'Минимум' in text


def test_very_long_title_still_fits_budget_and_keeps_tags_balanced():
    """Вырожденный случай: шапка длиннее бюджета — теги остаются закрытыми."""
    movie = _movie(title='Название' * 400, description=LONG_DESCRIPTION, rt_score=None)

    text, _ = build_movie_card(movie)

    assert len(text) <= MOVIE_CARD_CAPTION_LIMIT
    assert_html_balanced(text)
    assert text.startswith('🎬 <strong>')


def test_pathological_long_head_fits_budget_and_keeps_tags_balanced():
    """MINOR №2: патологический перерасход ШАПКИ (не описания).

    Очень длинный жанр + рейтинг при КОРОТКОМ названии: укорачивание названия
    не помогает, шапка всё равно длиннее бюджета. Без защитной обрезки через
    `_cut_html_safely` старый `_fit_card_head` возвращал head ДЛИННЕЕ limit, а
    голый срез `text[:limit]` в `build_movie_card` резал бы по живому —
    вплоть до разрыва `</strong>`/HTML-сущности. Жанр содержит `&`, чтобы
    экранирование дало сущности `&amp;` и голый срез мог их разорвать.
    """
    huge_genre = 'фантастика&приключения&боевик& ' * 200  # ~7000 символов, с «&»
    movie = _movie(title='X', genre=huge_genre, rating='7.8', description='', rt_score=91)
    # Sanity: шапка действительно не помещается даже без описания
    assert len(format_movie_card({**movie, 'description': ''})) > MOVIE_CARD_CAPTION_LIMIT

    # Детерминированный регресс-детектор: контракт `_fit_card_head`
    # «не длиннее limit». Старая реализация (только укорачивание названия)
    # возвращала head > limit; новая — обрезает по безопасной границе.
    head = _fit_card_head(movie, MOVIE_CARD_CAPTION_LIMIT)
    assert len(head) <= MOVIE_CARD_CAPTION_LIMIT

    text, _ = build_movie_card(movie)

    assert len(text) <= MOVIE_CARD_CAPTION_LIMIT
    assert_html_balanced(text)
    assert text.startswith('🎬 <strong>')
    # Срез прошёл по безопасной границе: нет оборванного «</stro» и нет
    # фрагментов битых HTML-сущностей вида «&am» (голый срез их бы оставил)
    assert '</stro' not in text or '</strong>' in text
    assert not _BROKEN_ENTITY_RE.search(text)


def test_cut_html_safely_does_not_break_entity_or_tag():
    assert _cut_html_safely('абвгде', 3) == 'абв'
    # Срез внутри сущности — откат к её началу
    assert _cut_html_safely('a&amp;b', 4) == 'a'
    # Срез внутри тега — откат к его началу
    assert _cut_html_safely('a<b>c', 3) == 'a'
    # Сущность целиком не влезает — откат к её началу, обрывка «&am» нет
    assert _cut_html_safely('a&amp;b', 5) == 'a'
    assert _cut_html_safely('a&amp;', 6) == 'a&amp;'
    assert _cut_html_safely('коротко', 100) == 'коротко'
    assert _cut_html_safely('текст', 0) == ''
    assert _cut_html_safely('текст', -5) == ''


# --- 6.3 Inline-клавиатура карточки ---


def test_card_keyboard_with_kinopoisk_url():
    _, keyboard = build_movie_card(_movie())

    buttons = _all_buttons(keyboard)
    assert [b.text for b in buttons] == [CARD_LINK_BUTTON_TEXT, CARD_SIMILAR_BUTTON_TEXT, CARD_SAVE_BUTTON_TEXT, CARD_BACK_BUTTON_TEXT]
    assert buttons[0].url == 'https://www.kinopoisk.ru/film/435/'
    assert buttons[1].callback_data == 'similar:435'
    assert buttons[2].callback_data == f'{SAVE_CALLBACK_PREFIX}435' == 'save:435'
    assert buttons[3].callback_data == CARD_BACK_CALLBACK == 'back:list'


def test_card_keyboard_without_valid_url_has_no_link_button():
    for url in (None, '', '   ', 'ftp://example.com/film', 'www.kinopoisk.ru/film/435/'):
        _, keyboard = build_movie_card(_movie(kinopoisk_url=url))

        buttons = _all_buttons(keyboard)
        assert [b.text for b in buttons] == [CARD_SIMILAR_BUTTON_TEXT, CARD_SAVE_BUTTON_TEXT, CARD_BACK_BUTTON_TEXT]
        assert buttons[0].callback_data == 'similar:435'
        assert buttons[1].callback_data == 'save:435'


def test_card_keyboard_limits_are_respected():
    _, keyboard = build_movie_card(_movie(id=123456789))

    for button in _all_buttons(keyboard):
        assert len(button.text) <= 64
        if button.callback_data:
            assert len(button.callback_data.encode('utf-8')) <= 64
        if button.url:
            assert button.url.startswith('http')


def test_card_keyboard_without_id_is_safe():
    """Нет id — callback остаётся валидным (0), пустой подписи нет."""
    _, keyboard = build_movie_card({'title': 'Без id'})

    buttons = _all_buttons(keyboard)
    assert buttons[-3].callback_data == 'similar:0'
    assert buttons[-2].callback_data == 'save:0'
    assert all(b.text for b in buttons)


# --- 6.1/6.4 Доставка карточки: одно сообщение и fallback ---


def test_card_with_poster_is_sent_as_single_photo_message():
    message = FakeMessage(chat=FakeChat())
    card_text, card_markup = build_movie_card(_movie(rt_score=91))

    asyncio.run(telegram_bot._send_result(message, {
        'response': card_text, 'reply_markup': card_markup, 'movie': _movie(rt_score=91),
    }))

    assert len(message.photos) == 1
    photo, kwargs = message.photos[0]
    assert photo == 'https://example.com/poster.jpg'
    assert kwargs['caption'] == card_text
    assert kwargs['parse_mode'] == 'HTML'
    assert kwargs['reply_markup'] is card_markup
    # Отдельного текстового сообщения с карточкой больше нет
    assert message.texts == []


def test_card_without_poster_falls_back_to_text():
    for poster in ('', None, 'ftp://example.com/p.jpg'):
        message = FakeMessage(chat=FakeChat())
        card_text, card_markup = build_movie_card(_movie(poster_url=poster))

        asyncio.run(telegram_bot._send_result(message, {
            'response': card_text, 'reply_markup': card_markup, 'movie': _movie(poster_url=poster),
        }))

        assert message.photos == []
        assert len(message.texts) == 1
        text, kwargs = message.texts[0]
        assert text == card_text
        assert kwargs['parse_mode'] == 'HTML'
        assert kwargs['reply_markup'] is card_markup


def test_broken_poster_falls_back_to_text_without_crash(caplog):
    message = FakeMessage(chat=FakeChat(), fail_photo=True)
    card_text, card_markup = build_movie_card(_movie(rt_score=91))

    with caplog.at_level(logging.WARNING):
        asyncio.run(telegram_bot._send_result(message, {
            'response': card_text, 'reply_markup': card_markup, 'movie': _movie(rt_score=91),
        }))

    assert message.photos == []
    assert len(message.texts) == 1
    assert message.texts[0][0] == card_text
    assert message.texts[0][1]['reply_markup'] is card_markup
    assert any('постер' in record.message for record in caplog.records)


def test_list_result_is_sent_as_text_without_poster():
    """У списка нет ключа movie — обычный текст с клавиатурой (без фото)."""
    message = FakeMessage(chat=FakeChat())
    dm = _manager()
    text, markup = dm.render_movie_list([_movie()], 'Заголовок')

    asyncio.run(telegram_bot._send_result(message, {'response': text, 'reply_markup': markup}))

    assert message.photos == []
    assert message.texts[0][0] == text
    assert message.texts[0][1]['reply_markup'] is markup


def test_process_and_reply_delivers_card_in_one_message(monkeypatch):
    dm = _manager()
    card_text, card_markup = build_movie_card(_INFO_MOVIE)
    dm.process_message = AsyncMock(return_value={
        'response': card_text, 'reply_markup': card_markup, 'movie': dict(_INFO_MOVIE),
    })
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    monkeypatch.setattr(telegram_bot.aiohttp, 'ClientSession', lambda *a, **kw: FakeHttpSession())
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda *a, **kw: None)

    message = FakeMessage(chat=FakeChat())
    update = SimpleNamespace(message=message, effective_user=FakeUser(777))
    asyncio.run(telegram_bot._process_and_reply(update, None, '777', 'расскажи о фильме Начало'))

    assert len(message.photos) == 1
    assert message.texts == []
    assert message.photos[0][1]['caption'] == card_text
    assert message.chat.actions == [{'action': 'typing'}]


def test_send_typing_without_chat_is_safe():
    """У фейков нет .chat — статус пропускается, отправка ответа не прерывается."""
    asyncio.run(telegram_bot._send_typing(object()))

    message = FakeMessage(chat=None)
    asyncio.run(telegram_bot._send_typing(message))
    assert message.chat is None


# --- 6.5 Оба рендерера дают идентичный текст ---


def test_info_intent_and_callback_render_identical_card(monkeypatch):
    """Info-интент и callback info: собирают карточку ЕДИНОЙ функцией."""
    # Путь info-интента (dialogue_manager.process_message)
    dm_info = _manager()
    dm_info.intent_classifier.classify_with_llm = AsyncMock(
        return_value={'intent': 'info', 'target_movie': 'Начало'}
    )
    dm_info.movie_agent.search_by_title = AsyncMock(return_value=[dict(_INFO_MOVIE)])
    info_result = asyncio.run(dm_info.process_message(None, 'u1', 'расскажи о фильме Начало'))

    # Путь callback info:{id} (telegram_bot.handle_movie_detail)
    dm_callback = _manager()
    _install_callback_mocks(monkeypatch, dm_callback)
    message = FakeMessage(chat=FakeChat())
    update = FakeCallbackUpdate('info:447301', message=message, from_user=FakeUser(777))
    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    assert message.texts == []
    assert len(message.photos) == 1
    caption = message.photos[0][1]['caption']
    assert caption == info_result['response']
    assert '🍅 91%' in caption
    # Клавиатура карточки присутствует в сообщении с постером
    assert [b.text for b in _all_buttons(message.photos[0][1]['reply_markup'])][0] == CARD_LINK_BUTTON_TEXT


# Один и тот же raw-документ Kinopoisk для ОБЕИХ путей (MAJOR №1): info-путь
# проходит РЕАЛЬНЫЙ `search_by_title`, callback — реальную сборку из HTTP-ответа.
_REAL_KP_DOC = {
    'id': 447301,
    'name': 'Начало',
    'year': 2010,
    'type': 'movie',
    'genres': [{'name': 'фантастика'}],
    'countries': [{'name': 'США'}],
    'rating': {'imdb': 8.8, 'kp': 8.7},
    'poster': {'url': 'https://example.com/poster.jpg'},
    # Описание длиннее прежнего среза [:500] — именно оно разъезжалось между
    # info-путём (резался в search_by_title) и callback-путём (не резался).
    'description': 'Описание фильма для проверки единой сборки карточки. ' * 20,
    'externalId': {'imdb': 'tt1375666'},
}


def test_info_path_uses_real_search_and_matches_callback_byte_for_byte(monkeypatch):
    """MAJOR №1 (регрессия): info-путь через РЕАЛЬНЫЙ `search_by_title`.

    Мок только на уровне `kinopoisk_client.search_movie_by_title` (и HTTP GET
    для callback) — НЕ готовым словарём movie. Для одного и того же raw-ответа
    текст карточки info-пути побайтово равен тексту callback-пути, а кнопка
    «🔗 Кинопоиск» присутствует в ОБОИХ путях. Прежний срез описания [:500] в
    `search_by_title` и отсутствие `kinopoisk_url` сломали бы оба утверждения.
    """
    assert len(_REAL_KP_DOC['description']) > 500  # иначе регрессия не покрыта

    # RT-обогащение в обоих путях — идентити (детерминированно, без сети):
    # ни один путь не добавляет rt_score, карточки остаются сравнимы побайтово.
    async def _identity_enrich(session, movies, client=None):
        return movies

    # --- Info-путь: РЕАЛЬНЫЙ search_by_title (мок только HTTP-поиска клиента) ---
    monkeypatch.setattr(movie_agent, 'enrich_movies_with_rt_scores', _identity_enrich)
    dm_info = _manager()
    dm_info.intent_classifier.classify_with_llm = AsyncMock(
        return_value={'intent': 'info', 'target_movie': 'Начало'}
    )
    dm_info.movie_agent.kinopoisk_client.search_movie_by_title = AsyncMock(
        return_value={'docs': [dict(_REAL_KP_DOC)]}
    )
    info_result = asyncio.run(dm_info.process_message(None, 'u1', 'расскажи о фильме Начало'))
    info_text = info_result['response']
    info_buttons = [b.text for b in _all_buttons(info_result['reply_markup'])]

    # --- Callback-путь info:{id}: тот же raw-документ из HTTP-ответа ---
    monkeypatch.setattr(telegram_bot, 'enrich_movies_with_rt_scores', _identity_enrich)
    dm_callback = _manager()
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm_callback)
    monkeypatch.setattr(
        telegram_bot.aiohttp, 'ClientSession',
        lambda *a, **kw: FakeHttpSession(dict(_REAL_KP_DOC), 200)
    )
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda *a, **kw: None)
    message = FakeMessage(chat=FakeChat())
    update = FakeCallbackUpdate('info:447301', message=message, from_user=FakeUser(777))
    asyncio.run(telegram_bot.handle_movie_detail(update, None))
    assert len(message.photos) == 1
    callback_text = message.photos[0][1]['caption']
    callback_buttons = [b.text for b in _all_buttons(message.photos[0][1]['reply_markup'])]

    # Побайтовая идентичность текста карточки в двух рендерерах
    assert info_text == callback_text
    # Кнопка «🔗 Кинопоиск» есть в ОБОИХ путях (kinopoisk_url заполнен везде)
    assert CARD_LINK_BUTTON_TEXT in info_buttons
    assert CARD_LINK_BUTTON_TEXT in callback_buttons
    # Описание НЕ срезано до 500: в карточке его больше (иначе регресс [:500])
    assert len(info_text) > 500


def test_info_intent_result_contains_card_keyboard():
    """Info-интент отдаёт и текст, и клавиатуру карточки (A4)."""
    dm = _manager()
    dm.intent_classifier.classify_with_llm = AsyncMock(
        return_value={'intent': 'info', 'target_movie': 'Начало'}
    )
    dm.movie_agent.search_by_title = AsyncMock(return_value=[dict(_INFO_MOVIE)])

    result = asyncio.run(dm.process_message(None, 'u1', 'расскажи о фильме Начало'))

    assert result['response'] == build_movie_card(dict(_INFO_MOVIE))[0]
    assert result['reply_markup'].to_dict() == build_movie_card_keyboard(dict(_INFO_MOVIE)).to_dict()


def test_callback_card_without_poster_is_text_with_keyboard(monkeypatch):
    """Пустой poster в ответе Kinopoisk — текстовая карточка с клавиатурой."""
    payload = {**_KP_PAYLOAD, 'poster': {'url': ''}}
    dm = _manager()
    _install_callback_mocks(monkeypatch, dm, payload=payload)

    message = FakeMessage(chat=FakeChat())
    update = FakeCallbackUpdate('info:447301', message=message, from_user=FakeUser(777))
    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    assert message.photos == []
    assert len(message.texts) == 1
    text, kwargs = message.texts[0]
    assert text == format_movie_card({
        **{k: v for k, v in _INFO_MOVIE.items() if k != 'poster_url'},
        'poster_url': '',
    })
    assert kwargs['parse_mode'] == 'HTML'
    assert _all_buttons(kwargs['reply_markup'])[1].callback_data == 'similar:447301'


def test_long_description_is_truncated_in_callback_path_too(monkeypatch):
    """Бюджет 1024 соблюдается и в callback-пути (среза [:500] больше нет)."""
    payload = {**_KP_PAYLOAD, 'description': LONG_DESCRIPTION}
    dm = _manager()
    _install_callback_mocks(monkeypatch, dm, payload=payload)

    message = FakeMessage(chat=FakeChat())
    update = FakeCallbackUpdate('info:447301', message=message, from_user=FakeUser(777))
    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    caption = message.photos[0][1]['caption']
    assert len(caption) <= MOVIE_CARD_CAPTION_LIMIT
    assert_html_balanced(caption)
    assert '🍅 91%' in caption


# --- 6.6 Маршрут similar: ---


def test_similar_callback_uses_existing_pipeline(monkeypatch):
    dm = _manager()
    session = dm.session_manager.get_session('777')
    session.last_movies = [_movie(id=435, title='Дюна'), _movie(id=436, title='Другой')]
    dm.movie_agent.recommend_movies = AsyncMock(return_value=[_movie(id=900, title='Похожий фильм')])
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    monkeypatch.setattr(telegram_bot.aiohttp, 'ClientSession', lambda *a, **kw: FakeHttpSession())
    tracked: List[str] = []
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda sid, *a, **kw: tracked.append(sid))

    update = FakeCallbackUpdate('similar:435', from_user=FakeUser(777))
    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    # Подбор выполнен существующим пайплайном similar-интента
    dm.movie_agent.recommend_movies.assert_awaited_once()
    kwargs = dm.movie_agent.recommend_movies.await_args.kwargs
    # База подбора — ВЫБРАННЫЙ фильм: жанр взят из него, а не доминирующий
    assert kwargs['genre_name'] == 'фантастика'
    assert tracked == ['tg:777']
    text, text_kwargs = update.callback_query.message.texts[0]
    assert 'Похожий фильм' in text
    assert text_kwargs['parse_mode'] == 'HTML'
    # Список похожих приходит с компактной клавиатурой A3
    assert text_kwargs['reply_markup'].inline_keyboard[-1][0].callback_data == 'alt:list'


def test_similar_callback_without_movie_in_session_is_friendly(monkeypatch):
    dm = _manager()
    dm.movie_agent.recommend_movies = AsyncMock()
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    monkeypatch.setattr(telegram_bot.aiohttp, 'ClientSession', lambda *a, **kw: FakeHttpSession())
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda *a, **kw: None)

    update = FakeCallbackUpdate('similar:435', from_user=FakeUser(777))
    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    dm.movie_agent.recommend_movies.assert_not_awaited()
    text, _ = update.callback_query.message.texts[0]
    assert 'Не нашёл этот фильм в истории' in text
    assert 'Напишите название фильма' in text


def test_similar_callback_with_bad_id_is_friendly(monkeypatch):
    dm = _manager()
    dm.movie_agent.recommend_movies = AsyncMock()
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    monkeypatch.setattr(telegram_bot.aiohttp, 'ClientSession', lambda *a, **kw: FakeHttpSession())
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda *a, **kw: None)

    update = FakeCallbackUpdate('similar:abc', from_user=FakeUser(777))
    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    dm.movie_agent.recommend_movies.assert_not_awaited()
    assert 'Не удалось определить фильм.' in update.callback_query.message.texts[0][0]


# --- 6.8 Маршрут back: ---


def test_back_callback_rebuilds_list_from_session(monkeypatch):
    dm = _manager()
    movies = [_movie(id=i, title=f'Фильм {i}') for i in range(1, 4)]
    dm.session_manager.get_session('777').last_movies = list(movies)
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)

    update = FakeCallbackUpdate('back:list', from_user=FakeUser(777))
    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    text, kwargs = update.callback_query.message.texts[0]
    expected_text, expected_markup = dm.render_movie_list(movies, telegram_bot.BACK_TO_LIST_HEADER)
    assert text == expected_text
    assert kwargs['reply_markup'].to_dict() == expected_markup.to_dict()
    assert kwargs['parse_mode'] == 'HTML'


def test_back_callback_with_empty_session_is_friendly(monkeypatch):
    dm = _manager()
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)

    update = FakeCallbackUpdate('back:list', from_user=FakeUser(777))
    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    text, kwargs = update.callback_query.message.texts[0]
    assert 'Список не сохранился' in text
    # Выход из тупика: главное меню предложено
    assert kwargs['reply_markup'] is not None


def test_back_callback_without_user_is_friendly(monkeypatch):
    dm = _manager()
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)

    update = FakeCallbackUpdate('back:list', from_user=None)
    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    assert 'Не удалось определить пользователя' in update.callback_query.message.texts[0][0]


# --- 6.7 Отказоустойчивость диспетчера ---


def test_unknown_prefix_replies_unknown_command():
    """B7: неизвестный префикс — объяснение с кнопками возврата, не голый текст."""
    update = FakeCallbackUpdate('menu:mood')

    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_UNKNOWN_CALLBACK
    # Выход из тупика: inline-кнопки «⬅️ К списку» и «🎲 Случайный фильм»
    callbacks = [b.callback_data for b in _all_buttons(kwargs['reply_markup'])]
    assert 'back:list' in callbacks
    assert telegram_bot.RANDOM_MOVIE_CALLBACK in callbacks
    # Индикация нажатия снята в любом случае
    assert update.callback_query.answer_calls == 1


def test_empty_callback_data_is_unknown_command():
    """B7: пустой callback_data — то же объяснение с кнопками выхода."""
    update = FakeCallbackUpdate('')

    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    assert update.callback_query.message.texts[0][0] == telegram_bot._ERROR_TEXT_UNKNOWN_CALLBACK


def test_dispatcher_returns_when_message_is_none():
    """NIT №6: у PTB `query.message` Optional — диспетчер выходит без падения.

    Без guard'а маршруты (и ветка «Неизвестная команда.») разыменовали бы
    `query.message.reply_text` и упали с AttributeError. Проверяем, что
    `answer()` всё равно вызван (индикация нажатия снята), а исключения нет.
    """
    update = FakeCallbackUpdate('info:447301')
    update.callback_query.message = None  # сообщение отсутствует (старое сообщение)

    asyncio.run(telegram_bot.handle_movie_detail(update, None))  # не должно бросить

    assert update.callback_query.answer_calls == 1


def test_bad_id_in_info_callback_is_handled(monkeypatch):
    dm = _manager()
    _install_callback_mocks(monkeypatch, dm)

    update = FakeCallbackUpdate('info:abc', from_user=FakeUser(777))
    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    text, kwargs = update.callback_query.message.texts[0]
    assert text == 'Не удалось определить фильм.'
    # B7: сообщение об ошибке содержит действие — inline-кнопки выхода
    callbacks = [b.callback_data for b in _all_buttons(kwargs['reply_markup'])]
    assert telegram_bot.RANDOM_MOVIE_CALLBACK in callbacks


def test_info_callback_api_error_does_not_crash(monkeypatch):
    """Kinopoisk ответил не-200 — ошибка класса «сеть» с повтором (B7).

    NIT №5: ветка не-200 не «мягкий тупик» (голый reply_text без parse_mode
    и меню); B7: классифицированный текст «Кинопоиск не отвечает» и
    inline-кнопки выхода, «🔄 Повторить» повторяет загрузку ЭТОЙ карточки.
    """
    dm = _manager()
    _install_callback_mocks(monkeypatch, dm, payload={}, status=500)

    update = FakeCallbackUpdate('info:447301', from_user=FakeUser(777))
    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_NETWORK
    assert kwargs.get('parse_mode') == 'HTML'
    callbacks = [b.callback_data for b in _all_buttons(kwargs['reply_markup'])]
    assert 'retry:info:447301' in callbacks  # повтор той же карточки
    assert telegram_bot.RANDOM_MOVIE_CALLBACK in callbacks  # выход, не dead-end


def test_routes_table_is_extensible():
    """Таблица маршрутов покрывает все префиксы A3/A4/B1/B3/B7/B4 (задел под B8)."""
    prefixes = [prefix for prefix, _ in telegram_bot._CALLBACK_ROUTES]

    assert prefixes == ['info:', 'alt:', 'similar:', 'back:', 'random:', 'mood:', 'retry:', 'page:', 'save:', 'unsave:', 'watchlist:', 'wpage:']
    for _, handler in telegram_bot._CALLBACK_ROUTES:
        assert asyncio.iscoroutinefunction(handler)
