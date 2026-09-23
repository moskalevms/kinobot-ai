"""Тесты A3: query-параметры search_movies в режиме «одобрено критиками».

Без сети: _make_request заменён заглушкой, фиксирующей параметры запроса.
"""
import asyncio

from kinopoisk_client import KinopoiskClient


class _CaptureClient(KinopoiskClient):
    """Клиент-заглушка: перехватывает параметры запроса вместо сети."""

    def __init__(self):
        super().__init__('test-kinopoisk-key')
        self.captured = None

    async def _make_request(self, session, url, params):
        self.captured = params
        return {'docs': []}


def _search(**kwargs):
    client = _CaptureClient()
    asyncio.run(client.search_movies(session=None, **kwargs))
    return client.captured


def test_critics_approved_api_params():
    """В режиме критиков — сортировка по fc, notNullFields и фильтр голосов."""
    params = _search(critics_approved=True)
    assert params['sortField'] == 'rating.filmCritics'
    assert params['sortType'] == -1
    assert params['notNullFields'] == 'rating.filmCritics'
    assert params['votes.filmCritics'] == '10-100000'


def test_default_mode_params_unchanged():
    """Регрессия: без critics_approved параметры прежние."""
    params = _search()
    assert params['sortField'] == 'rating.imdb'
    assert params['sortType'] == -1
    assert 'notNullFields' not in params
    assert 'votes.filmCritics' not in params


def test_critics_mode_keeps_existing_filters():
    """Режим критиков не ломает жанр/год/фильтры зрительских рейтингов."""
    params = _search(genre='драма', year=2020, imdb_rating_min=6.5,
                     kp_rating_min=6.0, critics_approved=True)
    assert params['genres.name'] == 'драма'
    assert params['year'] == 2020
    assert params['rating.imdb'] == '6.5-10'
    assert params['rating.kp'] == '6.0-10'
    assert params['sortField'] == 'rating.filmCritics'


def test_explicit_sort_by_overridden_in_critics_mode():
    """sortField в режиме критиков всегда — рейтинг критиков."""
    params = _search(sort_by='year', critics_approved=True)
    assert params['sortField'] == 'rating.filmCritics'


def test_no_rating_filter_by_fc_in_query():
    """Баг API: диапазонный фильтр по rating.filmCritics не отправляется
    (он молча игнорируется; порог fc >= 7 применяется локально в движке)."""
    params = _search(critics_approved=True)
    assert 'rating.filmCritics' not in params


# --- B3: externalId в selectFields (join-ключ IMDb ID для OMDb/RT) ---


def test_search_movies_select_fields_contains_external_id():
    """search_movies запрашивает внешние идентификаторы фильма."""
    params = _search()
    assert 'externalId' in params['selectFields']
    # Регрессия: прежние поля selectFields сохранены
    for field in ('id', 'name', 'year', 'genres', 'rating', 'votes',
                  'description', 'poster', 'persons', 'countries', 'type'):
        assert field in params['selectFields']


def test_search_recommendation_select_fields_contains_external_id():
    """search_recommendation (списки top250) запрашивает externalId."""
    client = _CaptureClient()
    asyncio.run(client.search_recommendation(session=None))
    assert 'externalId' in client.captured['selectFields']
    for field in ('id', 'name', 'year', 'genres', 'rating', 'votes',
                  'description', 'poster', 'countries'):
        assert field in client.captured['selectFields']


def test_search_movie_by_title_select_fields_contains_external_id():
    """search_movie_by_title запрашивает externalId."""
    client = _CaptureClient()
    asyncio.run(client.search_movie_by_title(session=None, title='Побег'))
    assert 'externalId' in client.captured['selectFields']
    for field in ('id', 'name', 'alternativeName', 'year', 'genres',
                  'rating', 'votes', 'description', 'poster', 'countries',
                  'type'):
        assert field in client.captured['selectFields']
