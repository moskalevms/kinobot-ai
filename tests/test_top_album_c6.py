"""Тесты C6: media group (альбом постеров) для топ-выдач.

Изменение add-top-media-group-album:
- `_build_top_album_media` — состав альбома (≤10 валидных постеров, минимум
  2, исходная нумерация, подписи `format_list_line` ≤1024, HTML);
- `_send_top_album` — fail-silent отправка (getattr reply_media_group,
  исключение → warning без падения);
- хуки доставки: `_run_dialogue_query` и `_process_and_reply` — список
  доставлен И альбом отправлен; без `is_top` — только список;
- `dialogue_manager`: топ-ветка initial-интента возвращает `is_top True`,
  search-ветка — без флага (False).

Фейки — из conftest (паттерн test_edit_callbacks_a5/test_movie_card_a4);
`reply_media_group` у общих фейков намеренно нет — альбом-сообщение
локальный класс (проверка защитного getattr в том числе).
"""
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from conftest import (
    BROKEN_ENTITY_RE,
    FakeCallbackUpdate,
    FakeChat,
    FakeHttpSession,
    FakeMessage,
    FakeQuery,
    FakeUser,
    POSTER_URL,
    assert_html_balanced,
    make_list_movies,
    make_manager,
    make_movie,
    run_coro,
)
from telegram import InputMediaPhoto

import telegram_bot

CAPTION_LIMIT = 1024


# --- Локальные фабрики и фейки ---


def _movie(index: int, poster: str = POSTER_URL, **overrides):
    """Фильм списка С постером (списочная фабрика conftest poster_url не даёт)."""
    base = dict(
        id=index,
        title=f'Фильм {index}',
        kinopoisk_url=f'https://www.kinopoisk.ru/film/{index}/',
        poster_url=poster,
    )
    base.update(overrides)
    return make_movie(**base)


def _top_result(movies, is_top=True):
    """Результат диалога топ-выдачи (форма возврата initial-интента)."""
    result = {
        'response': 'Топ фильмов',
        'reply_markup': None,
        'movies_list': movies,
        'needs_clarification': False,
    }
    if is_top is not None:
        result['is_top'] = is_top
    return result


class AlbumMessage(FakeMessage):
    """Сообщение с `reply_media_group`: записывает альбомы, умеет отказ."""

    def __init__(self, album_fail: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.album_fail = album_fail
        self.albums: list[tuple[Any, dict[str, Any]]] = []

    async def reply_media_group(self, media, **kwargs):
        if self.album_fail:
            raise RuntimeError('Telegram отклонил альбом')
        self.albums.append((media, kwargs))


class _MessageUpdate:
    """Update с `.message` для текстового пути (`_process_and_reply`)."""

    def __init__(self, message):
        self.message = message
        self.effective_user = FakeUser()


def _install_mocks(monkeypatch, result):
    """Подменить dialogue_manager/aiohttp/статистику для маршрутных тестов."""
    dm = SimpleNamespace(process_message=AsyncMock(return_value=result))
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    monkeypatch.setattr(telegram_bot.aiohttp, 'ClientSession', lambda *a, **kw: FakeHttpSession())
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda *a, **kw: None)
    return dm


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING and r.name == 'telegram']


# --- 3.1 `_build_top_album_media`: состав, лимиты, нумерация ---


def test_album_takes_first_ten_with_original_numbering():
    result = _top_result([_movie(i) for i in range(1, 14)])
    media = telegram_bot._build_top_album_media(result)
    assert len(media) == telegram_bot.TOP_ALBUM_LIMIT == 10
    assert all(isinstance(item, InputMediaPhoto) for item in media)
    # Нумерация подписей — исходные позиции 1..10 (совпадают с кнопками списка)
    for position, item in enumerate(media, start=1):
        assert item.caption.startswith(f'{position}.')
        assert f'Фильм {position}' in item.caption
        assert item.media == POSTER_URL
        assert item.parse_mode == 'HTML'


def test_album_skips_invalid_posters_without_renumbering():
    movies = [
        _movie(1),
        _movie(2, poster=''),                      # пустой URL — пропуск
        _movie(3),
        _movie(4, poster='/relative/poster.jpg'),  # относительный — пропуск
        _movie(5),
    ]
    media = telegram_bot._build_top_album_media(_top_result(movies))
    assert [item.caption.split('.')[0] for item in media] == ['1', '3', '5']
    assert len(media) == 3


def test_single_valid_poster_yields_no_album():
    movies = [_movie(1), _movie(2, poster='')]
    assert telegram_bot._build_top_album_media(_top_result(movies)) == []


def test_no_album_without_is_top_flag():
    movies = [_movie(i) for i in range(1, 4)]
    assert telegram_bot._build_top_album_media(_top_result(movies, is_top=False)) == []
    assert telegram_bot._build_top_album_media(_top_result(movies, is_top=None)) == []


def test_captions_contain_title_rating_and_within_limit():
    result = _top_result([_movie(i) for i in range(1, 14)])
    for item in telegram_bot._build_top_album_media(result):
        assert '⭐' in item.caption
        assert len(item.caption) <= CAPTION_LIMIT


# --- 3.2 Патологические строки: рез ≤1024 без разрыва HTML ---


def test_pathological_title_and_genre_caption_is_safe_html():
    movies = [
        _movie(1, title='Кадм & Хараппа <b>важно</b> "цитата" ' * 100,
               genre='драма, <мелодрама> & боевик ' * 50),
        _movie(2),
    ]
    media = telegram_bot._build_top_album_media(_top_result(movies))
    assert len(media) == telegram_bot.TOP_ALBUM_MIN
    caption = media[0].caption
    assert len(caption) <= CAPTION_LIMIT
    assert not BROKEN_ENTITY_RE.search(caption), 'HTML-сущность разорвана'
    assert_html_balanced(caption)
    # Пользовательские значения экранированы: голых «<b>важно</b>» из title нет
    assert '<b>важно</b>' not in caption


def test_huge_kinopoisk_url_caption_stays_balanced():
    movies = [
        _movie(1, kinopoisk_url='https://www.kinopoisk.ru/film/' + '9' * 3000 + '/'),
        _movie(2),
    ]
    media = telegram_bot._build_top_album_media(_top_result(movies))
    caption = media[0].caption
    assert len(caption) <= CAPTION_LIMIT
    assert not BROKEN_ENTITY_RE.search(caption)
    assert_html_balanced(caption)


# --- 3.3 `_send_top_album`: fail-silent контракт ---


def test_send_top_album_single_call_within_limit():
    message = AlbumMessage(chat=FakeChat())
    result = _top_result([_movie(i) for i in range(1, 14)])
    run_coro(telegram_bot._send_top_album(message, result))
    assert len(message.albums) == 1
    media = message.albums[0][0]
    assert telegram_bot.TOP_ALBUM_MIN <= len(media) <= telegram_bot.TOP_ALBUM_LIMIT


def test_send_top_album_swallows_exceptions(caplog):
    message = AlbumMessage(album_fail=True, chat=FakeChat())
    result = _top_result([_movie(i) for i in range(1, 4)])
    with caplog.at_level(logging.WARNING):
        run_coro(telegram_bot._send_top_album(message, result))
    assert any('альбом' in r.getMessage().lower() for r in _warnings(caplog))


def test_send_top_album_without_method_is_silent_noop(caplog):
    # У общего фейка conftest reply_media_group нет (старые клиенты/тесты)
    message = FakeMessage(chat=FakeChat())
    result = _top_result([_movie(i) for i in range(1, 4)])
    with caplog.at_level(logging.DEBUG):
        run_coro(telegram_bot._send_top_album(message, result))
    assert not _warnings(caplog)


def test_send_top_album_not_called_without_is_top():
    message = AlbumMessage(chat=FakeChat())
    result = _top_result([_movie(i) for i in range(1, 4)], is_top=False)
    run_coro(telegram_bot._send_top_album(message, result))
    assert message.albums == []


# --- 3.4 Интеграция маршрутов доставки ---


def test_run_dialogue_query_top_delivers_list_and_album(monkeypatch):
    result = _top_result([_movie(i) for i in range(1, 6)])
    _install_mocks(monkeypatch, result)
    message = AlbumMessage(chat=FakeChat())
    query = FakeQuery('top:50:movies', message=message, from_user=FakeUser())
    update = FakeCallbackUpdate('top:50:movies', message=message, from_user=FakeUser())
    run_coro(telegram_bot._run_dialogue_query(update, query, '777', 'топ 50 фильмов'))
    assert message.texts, 'Текстовый список не доставлен'
    assert len(message.albums) == 1
    assert len(message.albums[0][0]) == 5


def test_run_dialogue_query_without_is_top_delivers_list_only(monkeypatch):
    result = _top_result([_movie(i) for i in range(1, 6)], is_top=None)
    _install_mocks(monkeypatch, result)
    message = AlbumMessage(chat=FakeChat())
    query = FakeQuery('menu:top', message=message, from_user=FakeUser())
    run_coro(telegram_bot._run_dialogue_query(None, query, '777', 'посоветуй комедию'))
    assert message.texts, 'Текстовый список не доставлен'
    assert message.albums == []


def test_run_dialogue_query_album_failure_keeps_list(monkeypatch, caplog):
    result = _top_result([_movie(i) for i in range(1, 6)])
    _install_mocks(monkeypatch, result)
    message = AlbumMessage(album_fail=True, chat=FakeChat())
    query = FakeQuery('top:50:movies', message=message, from_user=FakeUser())
    with caplog.at_level(logging.WARNING):
        run_coro(telegram_bot._run_dialogue_query(None, query, '777', 'топ 50 фильмов'))
    assert message.texts, 'Список обязан остаться доставленным при сбое альбома'
    assert message.albums == []
    assert _warnings(caplog)


def test_process_and_reply_top_delivers_list_and_album(monkeypatch):
    result = _top_result([_movie(i) for i in range(1, 4)])
    _install_mocks(monkeypatch, result)
    message = AlbumMessage(chat=FakeChat())
    update = _MessageUpdate(message)
    context = SimpleNamespace(user_data={})
    run_coro(telegram_bot._process_and_reply(update, context, '777', 'топ фильмов'))
    assert message.texts, 'Текстовый список не доставлен'
    assert len(message.albums) == 1
    assert len(message.albums[0][0]) == 3


def test_process_and_reply_album_failure_keeps_list(monkeypatch, caplog):
    result = _top_result([_movie(i) for i in range(1, 4)])
    _install_mocks(monkeypatch, result)
    message = AlbumMessage(album_fail=True, chat=FakeChat())
    update = _MessageUpdate(message)
    context = SimpleNamespace(user_data={})
    with caplog.at_level(logging.WARNING):
        run_coro(telegram_bot._process_and_reply(update, context, '777', 'топ фильмов'))
    assert message.texts, 'Список обязан остаться доставленным при сбое альбома'
    assert _warnings(caplog)


def test_process_and_reply_without_is_top_delivers_list_only(monkeypatch):
    result = _top_result([_movie(i) for i in range(1, 4)], is_top=None)
    _install_mocks(monkeypatch, result)
    message = AlbumMessage(chat=FakeChat())
    update = _MessageUpdate(message)
    context = SimpleNamespace(user_data={})
    run_coro(telegram_bot._process_and_reply(update, context, '777', 'посоветуй комедию'))
    assert message.texts
    assert message.albums == []


# --- 3.5 dialogue_manager: флаг is_top по веткам ---


def test_top_branch_sets_is_top_flag():
    dm = make_manager()
    dm.intent_classifier.classify_with_llm = AsyncMock(
        return_value={'intent': 'initial', 'movie_type': 'movie'}
    )
    movies = make_list_movies(3)
    dm.movie_agent.recommend_movies = AsyncMock(return_value=movies)
    result = run_coro(dm.process_message(None, 'u1', 'топ фильмов'))
    assert result['is_top'] is True
    assert result['movies_list'] == movies
    assert result['needs_clarification'] is False


def test_search_branch_has_no_is_top_flag():
    dm = make_manager()
    dm.intent_classifier.classify_with_llm = AsyncMock(
        return_value={'intent': 'initial', 'genre': 'комедия', 'movie_type': 'movie'}
    )
    dm.movie_agent.recommend_movies = AsyncMock(return_value=make_list_movies(3))
    result = run_coro(dm.process_message(None, 'u1', 'посоветуй комедию'))
    assert result.get('is_top') is False


def test_top_error_result_has_no_is_top_flag():
    dm = make_manager()
    dm.intent_classifier.classify_with_llm = AsyncMock(
        return_value={'intent': 'initial', 'movie_type': 'movie'}
    )
    dm.movie_agent.recommend_movies = AsyncMock(return_value=[])
    result = run_coro(dm.process_message(None, 'u1', 'топ фильмов'))
    assert result.get('is_top') is None or result.get('is_top') is False
    assert result['needs_clarification'] is True
