"""Тесты B2: контекстные quick replies под каждым списком.

Изменение add-list-nav-and-pagination: ряд навигации под номерами — три
кнопки, привязанные к конкретной выдаче: «🔄 Другие» (`alt:list`,
существующий alternative-интент), «⬇️ Ещё 5» (`page:{offset}:{hash}`,
пагинация B3) и «🎲 Случайный» (`random:movie`, существующий маршрут B1).
Ряд проверяется на всех поверхностях списка: обычная выдача, похожие
(similar), другие варианты (alternative) и «⬅️ К списку» (back:).
Без сети; мок-паттерны и фабрики фильмов/запуска корутин (`_movie`,
`_movies`, `_run`) — общие хелперы `tests/conftest.py` (ревью n2).
"""
from unittest.mock import AsyncMock

import telegram_bot
from conftest import (
    FakeCallbackUpdate,
    FakeChat,
    FakeHttpSession,
    FakeMessage,
    FakeUser,
    all_buttons as _all_buttons,
    make_list_movie as _movie,
    make_list_movies as _movies,
    make_manager as _manager,
    number_buttons as _number_buttons,
    run_coro as _run,
)
from dialogue_manager import (
    BUTTON_TEXT_LIMIT,
    LIST_DISPLAY_LIMIT,
    LIST_MORE_BUTTON_TEXT,
    LIST_NAV_BUTTON_TEXT,
    LIST_NAV_CALLBACK,
    LIST_RANDOM_BUTTON_TEXT,
    PAGE_CALLBACK_PREFIX,
    RANDOM_MOVIE_CALLBACK,
    compute_list_hash,
)


def _nav_row(keyboard) -> list:
    """Последний ряд клавиатуры списка — ряд навигации (B2)."""
    return keyboard.inline_keyboard[-1]


def _assert_full_nav_row(nav_row: list, movies: list, offset: int = LIST_DISPLAY_LIMIT) -> None:
    """Три quick replies с ожидаемыми подписями и callback_data."""
    assert [b.text for b in nav_row] == [
        LIST_NAV_BUTTON_TEXT, LIST_MORE_BUTTON_TEXT, LIST_RANDOM_BUTTON_TEXT,
    ]
    assert nav_row[0].callback_data == LIST_NAV_CALLBACK == 'alt:list'
    assert nav_row[1].callback_data == f'{PAGE_CALLBACK_PREFIX}{offset}:{compute_list_hash(movies)}'
    assert nav_row[2].callback_data == RANDOM_MOVIE_CALLBACK == 'random:movie'


# --- 1. Ряд навигации на всех поверхностях списка ---


def test_nav_row_under_regular_search_result():
    """Обычная выдача (initial): под номерами три контекстные кнопки."""
    dm = _manager()
    dm.intent_classifier.classify_with_llm = AsyncMock(
        return_value={'intent': 'initial', 'genre': 'комедия', 'movie_type': 'movie'}
    )
    movies = _movies(13)
    dm.movie_agent.recommend_movies = AsyncMock(return_value=movies)

    result = _run(dm.process_message(None, 'u1', 'посоветуй комедию'))

    keyboard = result['reply_markup']
    # Два ряда номеров (4 + 1) + ряд навигации — раскладка A3 сохранена
    assert len(keyboard.inline_keyboard) == 3
    assert len(_number_buttons(keyboard)) == LIST_DISPLAY_LIMIT
    # Hash в кнопке пагинации — отпечаток ПОЛНОЙ выдачи (13 фильмов),
    # offset следующей страницы — 5
    _assert_full_nav_row(_nav_row(keyboard), movies)


def test_nav_row_under_similar_result():
    """Похожие (similar): новый список получает тот же ряд навигации."""
    dm = _manager()
    session = dm.session_manager.get_session('u1')
    session.last_movies = [_movie(id=435, title='Дюна')]
    similar_movies = _movies(8)
    dm.movie_agent.recommend_movies = AsyncMock(return_value=similar_movies)

    result = _run(dm.find_similar_by_id(FakeHttpSession(), 'u1', 435))

    keyboard = result['reply_markup']
    # Пагинация считается от НОВОЙ выдачи похожих (8 фильмов): следующая
    # страница offset=5, hash — отпечаток списка похожих
    _assert_full_nav_row(_nav_row(keyboard), result['movies_list'])
    assert result['movies_list'] == similar_movies[:13]


def test_nav_row_under_alternative_result():
    """Другие варианты (alternative): ряд навигации у новой выдачи."""
    dm = _manager()
    session = dm.session_manager.get_session('u1')
    session.last_params = {'genre': 'комедия', 'movie_type': 'movie'}
    session.last_movies = [_movie(id=999, title='Старый фильм')]
    dm.intent_classifier.classify_with_llm = AsyncMock(return_value={'intent': 'alternative'})
    fresh = [_movie(id=i, title=f'Фильм {i}') for i in range(100, 110)]
    dm.movie_agent.recommend_movies = AsyncMock(return_value=fresh)

    result = _run(dm.process_message(None, 'u1', 'посоветуй другие фильмы'))

    _assert_full_nav_row(_nav_row(result['reply_markup']), result['movies_list'])


def test_nav_row_under_back_to_list(monkeypatch):
    """«⬅️ К списку» (back:): пересобранный список — с тем же рядом."""
    dm = _manager()
    movies = _movies(13)
    dm.session_manager.get_session('777').last_movies = list(movies)
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    message = FakeMessage(chat=FakeChat())
    update = FakeCallbackUpdate('back:list', message=message, from_user=FakeUser(777))

    _run(telegram_bot.handle_movie_detail(update, None))

    assert len(message.texts) == 1
    keyboard = message.texts[0][1]['reply_markup']
    _assert_full_nav_row(_nav_row(keyboard), movies)


def test_short_list_has_no_more_button():
    """Выдача ≤5 фильмов: «⬇️ Ещё 5» нет, остальные две кнопки на месте."""
    dm = _manager()
    movies = _movies(3)

    _, keyboard = dm._generate_list_response(movies, 'Заголовок')

    nav_row = _nav_row(keyboard)
    assert [b.text for b in nav_row] == [LIST_NAV_BUTTON_TEXT, LIST_RANDOM_BUTTON_TEXT]
    assert nav_row[0].callback_data == 'alt:list'
    assert nav_row[1].callback_data == RANDOM_MOVIE_CALLBACK
    # Кнопки пагинации нет ни в одном ряду
    assert not any(
        (b.callback_data or '').startswith(PAGE_CALLBACK_PREFIX)
        for b in _all_buttons(keyboard)
    )


# --- 2. Кнопки ряда вызывают существующие маршруты ---


def test_nav_alt_button_runs_alternative_pipeline(monkeypatch):
    """«🔄 Другие» из ряда навигации — тот же пайплайн, что reply-кнопка."""
    dm = _manager()
    dm.process_message = AsyncMock(return_value={
        'response': '<strong>Вот другие варианты:</strong>\n',
        'reply_markup': None,
    })
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    monkeypatch.setattr(telegram_bot.aiohttp, 'ClientSession', lambda *a, **kw: FakeHttpSession())
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda *a, **kw: None)
    # Callback берём ИЗ собранного ряда навигации, а не литералом
    _, keyboard = _manager()._generate_list_response(_movies(13), 'Заголовок')
    alt_callback = _nav_row(keyboard)[0].callback_data

    message = FakeMessage(chat=FakeChat())
    update = FakeCallbackUpdate(alt_callback, message=message, from_user=FakeUser(777))
    _run(telegram_bot.handle_movie_detail(update, None))

    dm.process_message.assert_awaited_once()
    assert dm.process_message.await_args.args[1:] == ('777', telegram_bot.OTHER_VARIANTS_QUERY)
    assert message.texts[0][0] == '<strong>Вот другие варианты:</strong>\n'


def test_nav_random_button_runs_random_route(monkeypatch):
    """«🎲 Случайный» из ряда навигации — существующий маршрут random:."""
    dm = _manager()
    movie = _movie(id=555, title='Случайный фильм')
    dm.get_random_movie = AsyncMock(return_value={
        'response': 'карточка', 'reply_markup': None, 'movie': movie, 'movies_list': [movie],
    })
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    monkeypatch.setattr(telegram_bot.aiohttp, 'ClientSession', lambda *a, **kw: FakeHttpSession())
    tracked: list = []
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda sid, *a, **kw: tracked.append(sid))
    _, keyboard = _manager()._generate_list_response(_movies(13), 'Заголовок')
    random_callback = _nav_row(keyboard)[2].callback_data

    message = FakeMessage(chat=FakeChat())
    update = FakeCallbackUpdate(random_callback, message=message, from_user=FakeUser(777))
    _run(telegram_bot.handle_movie_detail(update, None))

    dm.get_random_movie.assert_awaited_once()
    assert dm.get_random_movie.await_args.args[1] == '777'
    assert tracked == ['tg:777']
    assert message.texts[0][0] == 'карточка'


# --- 3. Лимиты Bot API ---


def test_nav_buttons_fit_telegram_limits():
    """Подписи ≤64 символов, callback_data ≤64 байт (включая page:)."""
    dm = _manager()
    movies = [_movie(id=10**15 + i, title='X' * 100) for i in range(1, 14)]

    _, keyboard = dm._generate_list_response(movies, 'Заголовок')

    for button in _all_buttons(keyboard):
        assert len(button.text) <= BUTTON_TEXT_LIMIT
        assert len(button.callback_data.encode('utf-8')) <= 64


def test_page_callback_is_short_even_with_big_offset():
    """Длина page-callback не зависит от данных фильмов: только offset+hash."""
    callback = f'{PAGE_CALLBACK_PREFIX}{10**3 - 5}:{compute_list_hash(_movies(13))}'

    assert len(compute_list_hash(_movies(13))) == 8
    assert len(callback.encode('utf-8')) <= 64
