"""Тесты B3: пагинация списка выдачи «⬇️ Ещё 5» (add-list-nav-and-pagination).

Без сети: срез страницы берётся СТРОГО из `session.last_movies` (in-memory
кэш движка `_search_cache` с TTL ~45 с не читается — ловушка AGENTS.md и
критерий приёмки B3 «работает через >45 с»), нумерация строк и кнопок
продолжается с offset, доставка — редактированием сообщения (A5), на
последней странице «⬇️ Ещё 5» скрыта и выводится явный текст исчерпания
БЕЗ нового запроса к движку. Проверки: идемпотентность повторного тапа,
устаревший hash (дружелюбно, B7), пустая сессия, некорректный
callback_data, `retry:page:…`, возврат «⬅️ К списку» на ТУ ЖЕ страницу
(снапшот в context.user_data) и на первую при смене выдачи, нейтральный
заголовок первой страницы при clamp offset за границами выдачи (ревью m1).
Мок-паттерны и фабрики фильмов/запуска корутин (`_movie`, `_movies`, `_run`)
— общие хелперы `tests/conftest.py` (ревью n2).
"""
import logging
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock

import telegram_bot
from conftest import (
    FakeCallbackUpdate,
    FakeUser,
    all_buttons as _all_buttons,
    editable_message as _editable_message,
    install_callback_mocks as _install_callback_mocks,
    make_list_movie as _movie,
    make_list_movies as _movies,
    make_manager as _manager,
    number_buttons as _number_buttons,
    run_coro as _run,
)
from dialogue_manager import (
    LIST_END_TEXT,
    LIST_MORE_BUTTON_TEXT,
    PAGE_CALLBACK_PREFIX,
    clamp_page_offset,
    compute_list_hash,
)
from telegram.error import BadRequest

NOT_MODIFIED_ERROR = (
    'Message is not modified: specified new message content and reply markup '
    'are exactly the same as a current content of the message'
)


def _warnings(caplog) -> List[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING and r.name == 'telegram']


def _page_callback(movies: list, offset: int) -> str:
    return f'{PAGE_CALLBACK_PREFIX}{offset}:{compute_list_hash(movies)}'


def _dispatch(monkeypatch, dm, data: str, message=None, context=None, from_user=FakeUser(777)):
    """Прогнать callback через диспетчер с подменённым менеджером диалога."""
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    msg = message if message is not None else _editable_message(photo=())
    update = FakeCallbackUpdate(data, message=msg, from_user=from_user)
    _run(telegram_bot.handle_movie_detail(update, context))
    return msg, update


# --- 1. Хелперы пагинации (чистые функции) ---


def test_compute_list_hash_is_deterministic_and_order_sensitive():
    movies = _movies(13)

    assert compute_list_hash(movies) == compute_list_hash(list(movies))
    assert len(compute_list_hash(movies)) == 8
    # Другой состав/порядок — другой отпечаток (защита устаревших кнопок)
    assert compute_list_hash(movies) != compute_list_hash(movies[::-1])
    assert compute_list_hash(movies) != compute_list_hash(_movies(12))


def test_clamp_page_offset_normalizes_invalid_values():
    assert clamp_page_offset(13, 0) == 0
    assert clamp_page_offset(13, 5) == 5
    assert clamp_page_offset(13, 10) == 10
    # Некратный offset снапится на границу страницы
    assert clamp_page_offset(13, 7) == 5
    # Отрицательный, выход за список и пустой список — первая страница
    assert clamp_page_offset(13, -5) == 0
    assert clamp_page_offset(13, 13) == 0
    assert clamp_page_offset(0, 5) == 0


# --- 2. Срез страницы из сессии и продолжение нумерации ---


def test_more_button_renders_next_page_from_session(monkeypatch):
    """«⬇️ Ещё 5»: строки 6–10, кнопки 6️⃣…🔟, доставка редактированием."""
    dm = _manager()
    movies = _movies(13)
    dm.session_manager.get_session('777').last_movies = list(movies)
    dm.movie_agent.recommend_movies = AsyncMock()
    _install_callback_mocks(monkeypatch, dm)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, _page_callback(movies, 5), message=message)

    message.edit_text.assert_awaited_once()
    text = message.edit_text.await_args.args[0]
    kwargs = message.edit_text.await_args.kwargs
    assert kwargs['parse_mode'] == 'HTML'
    # Заголовок продолжения + строки 6..10, фильмов 1–5 на странице нет
    assert text.startswith(f'<strong>{telegram_bot.PAGE_CONTINUATION_HEADER}</strong>')
    for i in range(6, 11):
        assert f'{i}. <b><a' in text and f'>Фильм {i}</a></b>' in text
    assert 'Фильм 5<' not in text
    # Кнопки-номера продолжают нумерацию и ведут на info:{id} тех же фильмов
    keyboard = kwargs['reply_markup']
    numbers = _number_buttons(keyboard)
    assert [b.text for b in numbers] == ['6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']
    assert [b.callback_data for b in numbers] == [f'info:{i}' for i in range(6, 11)]
    # Ряд навигации: следующая страница 10, те же «🔄 Другие» и «🎲 Случайный»
    nav = keyboard.inline_keyboard[-1]
    assert nav[1].text == LIST_MORE_BUTTON_TEXT
    assert nav[1].callback_data == _page_callback(movies, 10)
    # Редактирование, а не новое сообщение (A5); движок не вызывался
    message.reply_text.assert_not_awaited()
    message.reply_photo.assert_not_awaited()
    dm.movie_agent.recommend_movies.assert_not_awaited()


def test_pagination_reads_session_not_search_cache(monkeypatch):
    """Источник — `session.last_movies`: `_search_cache` не читается вовсе.

    Кэш движка in-memory с TTL ~45 с (ловушка AGENTS.md): подменяем его
    объектом, который падает при ЛЮБОМ обращении, — маршрут страницы
    обязан остаться зелёным (критерий приёмки B3 «работает, когда кэш
    протух»).
    """

    class _BoomCache(dict):
        def __getitem__(self, key):
            raise AssertionError('пагинация не должна читать _search_cache')

        def get(self, *args, **kwargs):
            raise AssertionError('пагинация не должна читать _search_cache')

        def __contains__(self, item):
            raise AssertionError('пагинация не должна читать _search_cache')

    dm = _manager()
    movies = _movies(13)
    dm.session_manager.get_session('777').last_movies = list(movies)
    dm.movie_agent._search_cache = _BoomCache()
    _install_callback_mocks(monkeypatch, dm)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, _page_callback(movies, 5), message=message)

    message.edit_text.assert_awaited_once()
    assert '>Фильм 6</a></b>' in message.edit_text.await_args.args[0]


def test_last_page_hides_more_button_and_shows_end_text(monkeypatch):
    """Последняя страница (11–13): «⬇️ Ещё 5» скрыта, текст исчерпания есть."""
    dm = _manager()
    movies = _movies(13)
    dm.session_manager.get_session('777').last_movies = list(movies)
    dm.movie_agent.recommend_movies = AsyncMock()
    _install_callback_mocks(monkeypatch, dm)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, _page_callback(movies, 10), message=message)

    text = message.edit_text.await_args.args[0]
    keyboard = message.edit_text.await_args.kwargs['reply_markup']
    for i in range(11, 14):
        assert f'>Фильм {i}</a></b>' in text
    assert LIST_END_TEXT in text
    # Кнопки пагинации нет ни в одном ряду; остальные quick replies на месте
    assert not any(
        (b.callback_data or '').startswith(PAGE_CALLBACK_PREFIX)
        for b in _all_buttons(keyboard)
    )
    assert [b.text for b in keyboard.inline_keyboard[-1]] == ['🔄 Другие', '🎲 Случайный']
    # Исчерпание НЕ делает новый запрос к движку (design.md D4)
    dm.movie_agent.recommend_movies.assert_not_awaited()


def test_first_page_of_short_list_has_no_end_text():
    """Короткая выдача на первой странице: текста исчерпания нет (offset=0)."""
    dm = _manager()
    movies = _movies(3)

    text, keyboard = dm.render_movie_list(movies, 'Заголовок')

    assert LIST_END_TEXT not in text
    assert not any(
        (b.callback_data or '').startswith(PAGE_CALLBACK_PREFIX)
        for b in _all_buttons(keyboard)
    )


def test_offset_is_clamped_to_page_boundary(monkeypatch):
    """«Рукодельный» offset=7 снапится на границу страницы (offset=5)."""
    dm = _manager()
    movies = _movies(13)
    dm.session_manager.get_session('777').last_movies = list(movies)
    _install_callback_mocks(monkeypatch, dm)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, _page_callback(movies, 7), message=message)

    text = message.edit_text.await_args.args[0]
    # Страница началась с 6 (снап), а не с 8: фильмов 11–12 на ней нет
    assert '6. <b><a' in text
    assert '>Фильм 6</a></b>' in text
    assert 'Фильм 12<' not in text


def test_out_of_range_offset_falls_back_to_first_page(monkeypatch):
    """Offset за пределами выдачи (hash совпал) — первая страница, не срез-пустышка.

    «Рукодельный» callback (offset ≥ total либо отрицательный) клампится в 0
    (design.md D3), а первая страница НЕ может называться «Продолжение
    списка:» — рендерится нейтральный заголовок `BACK_TO_LIST_HEADER`
    (ревью m1).
    """
    dm = _manager()
    movies = _movies(13)
    dm.session_manager.get_session('777').last_movies = list(movies)
    _install_callback_mocks(monkeypatch, dm)

    for offset in (99, -5):
        message = _editable_message(photo=())
        _dispatch(monkeypatch, dm, _page_callback(movies, offset), message=message)

        message.edit_text.assert_awaited_once()
        text = message.edit_text.await_args.args[0]
        keyboard = message.edit_text.await_args.kwargs['reply_markup']
        assert '>Фильм 1</a></b>' in text
        assert text.startswith(f'<strong>{telegram_bot.BACK_TO_LIST_HEADER}</strong>')
        assert not text.startswith(f'<strong>{telegram_bot.PAGE_CONTINUATION_HEADER}</strong>')
        # Кнопка «⬇️ Ещё 5» первой страницы ведёт на offset 5, а не на 100/-5
        assert keyboard.inline_keyboard[-1][1].callback_data == _page_callback(movies, 5)


# --- 3. Идемпотентность повторного тапа ---


def test_double_tap_same_page_is_silent(monkeypatch, caplog):
    """Повторный тап «⬇️ Ещё 5»: «not modified» молча, offset не сбивается."""
    dm = _manager()
    movies = _movies(13)
    dm.session_manager.get_session('777').last_movies = list(movies)
    _install_callback_mocks(monkeypatch, dm)
    message = _editable_message(photo=())
    message.edit_text.side_effect = BadRequest(NOT_MODIFIED_ERROR)

    with caplog.at_level(logging.DEBUG):
        _dispatch(monkeypatch, dm, _page_callback(movies, 5), message=message)

    assert not _warnings(caplog)
    message.reply_text.assert_not_awaited()
    message.reply_photo.assert_not_awaited()
    message.delete.assert_not_awaited()
    assert message.chat is not None


def test_same_page_renders_byte_identical(monkeypatch):
    """Детерминированность: два рендера одной страницы побайтово равны.

    Именно это гарантирует «message is not modified» (а не дубль) при
    повторном тапе — offset не сбивается.
    """
    dm = _manager()
    movies = _movies(13)

    first = dm.render_movie_list(movies, telegram_bot.PAGE_CONTINUATION_HEADER, offset=5)
    second = dm.render_movie_list(list(movies), telegram_bot.PAGE_CONTINUATION_HEADER, offset=5)

    assert first[0] == second[0]
    assert first[1].to_dict() == second[1].to_dict()


# --- 4. Устаревший hash, пустая сессия, некорректные данные (B7) ---


def test_stale_hash_is_friendly_and_does_not_edit(monkeypatch):
    """Выдача сменилась: объяснение + выходы, сообщение списка не правится."""
    dm = _manager()
    dm.session_manager.get_session('777').last_movies = _movies(13)
    _install_callback_mocks(monkeypatch, dm)
    message = _editable_message(photo=())
    stale = f'{PAGE_CALLBACK_PREFIX}5:deadbeef'

    _, update = _dispatch(monkeypatch, dm, stale, message=message)

    message.edit_text.assert_not_awaited()
    message.reply_text.assert_awaited_once()
    text = message.reply_text.await_args.args[0]
    kwargs = message.reply_text.await_args.kwargs
    assert text == telegram_bot._ERROR_TEXT_STALE_PAGE
    callbacks = [b.callback_data for b in _all_buttons(kwargs['reply_markup'])]
    assert 'back:list' in callbacks and telegram_bot.RANDOM_MOVIE_CALLBACK in callbacks
    assert update.callback_query.answer_calls == 1


def test_empty_session_page_callback_is_friendly(monkeypatch):
    """Сессия пуста (рестарт/истекла): дружелюбный выход, не исключение."""
    dm = _manager()
    _install_callback_mocks(monkeypatch, dm)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, f'{PAGE_CALLBACK_PREFIX}5:abcd1234', message=message)

    message.edit_text.assert_not_awaited()
    text = message.reply_text.await_args.args[0]
    kwargs = message.reply_text.await_args.kwargs
    assert text == telegram_bot._ERROR_TEXT_STALE_PAGE
    assert kwargs['reply_markup'] is not None


def test_malformed_page_callback_is_friendly(monkeypatch, caplog):
    """`page:abc:x` и `page:5` (нет hash) — объяснение с выходами, warning."""
    dm = _manager()
    dm.session_manager.get_session('777').last_movies = _movies(13)
    _install_callback_mocks(monkeypatch, dm)

    for data in ('page:abc:ffff', 'page:5', 'page::ffff'):
        message = _editable_message(photo=())
        with caplog.at_level(logging.WARNING):
            _dispatch(monkeypatch, dm, data, message=message)
        message.edit_text.assert_not_awaited()
        text = message.reply_text.await_args.args[0]
        kwargs = message.reply_text.await_args.kwargs
        assert 'Не удалось открыть страницу' in text
        callbacks = [b.callback_data for b in _all_buttons(kwargs['reply_markup'])]
        assert telegram_bot.RANDOM_MOVIE_CALLBACK in callbacks  # выход, не dead-end
    assert _warnings(caplog)


def test_page_callback_without_user_is_friendly(monkeypatch):
    """Без user_id — дружелюбный ответ (паттерн прочих маршрутов)."""
    dm = _manager()
    dm.session_manager.get_session('777').last_movies = _movies(13)
    _install_callback_mocks(monkeypatch, dm)
    movies = _movies(13)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, _page_callback(movies, 5), message=message, from_user=None)

    message.edit_text.assert_not_awaited()
    text = message.reply_text.await_args.args[0]
    assert 'Не удалось определить пользователя' in text


def test_retry_page_repeats_same_page(monkeypatch):
    """«🔄 Повторить» из ошибки маршрута: `retry:page:5:{hash}` листает ту же страницу."""
    dm = _manager()
    movies = _movies(13)
    dm.session_manager.get_session('777').last_movies = list(movies)
    _install_callback_mocks(monkeypatch, dm)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, f'retry:{_page_callback(movies, 5)}', message=message)

    message.edit_text.assert_awaited_once()
    assert '>Фильм 6</a></b>' in message.edit_text.await_args.args[0]


# --- 5. «⬅️ К списку» возвращает на ТУ ЖЕ страницу (design.md D5) ---


def test_back_returns_to_same_page(monkeypatch):
    """info: со второй страницы → «⬅️ К списку» → снова строки 6–10."""
    dm = _manager()
    movies = _movies(13)
    dm.session_manager.get_session('777').last_movies = list(movies)
    _install_callback_mocks(monkeypatch, dm)
    context = SimpleNamespace(user_data={})

    # Тап «⬇️ Ещё 5» — снапшот страницы записан в user_data
    page_message = _editable_message(photo=())
    _dispatch(monkeypatch, dm, _page_callback(movies, 5), message=page_message, context=context)
    assert context.user_data[telegram_bot._LIST_PAGE_KEY] == {
        'offset': 5, 'hash': compute_list_hash(movies),
    }

    # «⬅️ К списку» из карточки — возврат на ТУ ЖЕ страницу
    back_message = _editable_message(photo=())
    _dispatch(monkeypatch, dm, 'back:list', message=back_message, context=context)
    text = back_message.edit_text.await_args.args[0]
    assert text.startswith(f'<strong>{telegram_bot.BACK_TO_LIST_HEADER}</strong>')
    for i in range(6, 11):
        assert f'>Фильм {i}</a></b>' in text
    assert 'Фильм 1<' not in text


def test_back_after_session_replaced_returns_first_page(monkeypatch):
    """Выдача перезаписана новым подбором: hash разошёлся — первая страница."""
    dm = _manager()
    old_movies = _movies(13)
    session = dm.session_manager.get_session('777')
    session.last_movies = list(old_movies)
    _install_callback_mocks(monkeypatch, dm)
    context = SimpleNamespace(user_data={
        telegram_bot._LIST_PAGE_KEY: {'offset': 5, 'hash': compute_list_hash(old_movies)},
    })
    # Новый подбор перезаписал сессию (например, «🔄 Другие»)
    session.last_movies = [_movie(id=500 + i, title=f'Новый {i}') for i in range(1, 9)]

    message = _editable_message(photo=())
    _dispatch(monkeypatch, dm, 'back:list', message=message, context=context)

    text = message.edit_text.await_args.args[0]
    assert '>Новый 1</a></b>' in text
    assert 'Фильм 6<' not in text
    # Снапшот перезаписан фактическим состоянием (offset 0, свежий hash)
    assert context.user_data[telegram_bot._LIST_PAGE_KEY] == {
        'offset': 0, 'hash': compute_list_hash(session.last_movies),
    }


def test_back_without_context_still_works(monkeypatch):
    """context=None (тесты/сбой): возврат на первую страницу, без падения."""
    dm = _manager()
    dm.session_manager.get_session('777').last_movies = _movies(13)
    _install_callback_mocks(monkeypatch, dm)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, 'back:list', message=message, context=None)

    text = message.edit_text.await_args.args[0]
    assert '>Фильм 1</a></b>' in text


def test_garbage_snapshot_is_ignored(monkeypatch):
    """Мусор в снапшоте (не dict / нечисловой offset) — первая страница."""
    dm = _manager()
    movies = _movies(13)
    dm.session_manager.get_session('777').last_movies = list(movies)
    _install_callback_mocks(monkeypatch, dm)

    for garbage in ({'offset': 'abc', 'hash': compute_list_hash(movies)}, 'строка', None):
        context = SimpleNamespace(user_data={telegram_bot._LIST_PAGE_KEY: garbage})
        message = _editable_message(photo=())
        _dispatch(monkeypatch, dm, 'back:list', message=message, context=context)
        assert '>Фильм 1</a></b>' in message.edit_text.await_args.args[0]


# --- 6. Учёт клика и сбой хранилища ---


def test_page_click_is_tracked(monkeypatch):
    """Клик по «⬇️ Ещё 5» учитывается в клиентской статистике."""
    dm = _manager()
    movies = _movies(13)
    dm.session_manager.get_session('777').last_movies = list(movies)
    _install_callback_mocks(monkeypatch, dm)
    tracked: list = []
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda sid, *a, **kw: tracked.append(sid))

    _dispatch(monkeypatch, dm, _page_callback(movies, 5))

    assert tracked == ['tg:777']


def test_storage_failure_gives_retry_of_same_page(monkeypatch):
    """Сбой хранилища — классифицированная ошибка с retry:page:{offset}:{hash}."""
    dm = _manager()
    movies = _movies(13)
    dm.session_manager.get_session('777').last_movies = list(movies)

    def _boom(user_id):
        raise RuntimeError('сеть недоступна')

    dm.session_manager.get_session = _boom  # type: ignore[method-assign]
    _install_callback_mocks(monkeypatch, dm)
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    message = _editable_message(photo=())

    _dispatch(monkeypatch, dm, _page_callback(movies, 5), message=message)

    message.edit_text.assert_not_awaited()
    text = message.reply_text.await_args.args[0]
    kwargs = message.reply_text.await_args.kwargs
    # RuntimeError без маркеров сети/LLM — резервный класс (B7), повтор цели
    assert text == telegram_bot._ERROR_TEXT_GENERIC
    callbacks = [b.callback_data for b in _all_buttons(kwargs['reply_markup'])]
    assert f'retry:{_page_callback(movies, 5)}' in callbacks
    assert len(f'retry:{_page_callback(movies, 5)}'.encode('utf-8')) <= 64
