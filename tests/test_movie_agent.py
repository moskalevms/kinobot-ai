import asyncio
import time
from unittest.mock import AsyncMock

from movie_agent import MovieAgent


def test_cache_keys_isolated_by_user():
    agent = MovieAgent()
    key_a = agent._get_cache_key('userA', 'комедия', None, None, None, None, None, 6.0, 8, 'movie', None)
    key_b = agent._get_cache_key('userB', 'комедия', None, None, None, None, None, 6.0, 8, 'movie', None)
    assert key_a != key_b


def test_cache_keys_isolated_by_critics_mode():
    """A3: режим «одобрено критиками» не смешивается с обычным в кэше."""
    agent = MovieAgent()
    key_plain = agent._get_cache_key('u', 'драма', None, None, None, None, None, 6.0, 8, 'movie', None)
    key_critics = agent._get_cache_key('u', 'драма', None, None, None, None, None, 6.0, 8, 'movie', None, True)
    assert key_plain != key_critics


def test_clear_cache_scoped_to_user():
    agent = MovieAgent()
    agent._search_cache['userA_комедия'] = (['a'], time.time())
    agent._search_cache['userB_комедия'] = (['b'], time.time())

    agent.clear_cache(user_id='userA')

    assert 'userA_комедия' not in agent._search_cache
    assert 'userB_комедия' in agent._search_cache


def test_clear_cache_all_without_user_id():
    agent = MovieAgent()
    agent._search_cache['userA_комедия'] = (['a'], time.time())
    agent._search_cache['userB_комедия'] = (['b'], time.time())

    agent.clear_cache()

    assert agent._search_cache == {}


# --- B3: imdb_id в карточке search_by_title (join-ключ для OMDb/RT) ---


def _agent_with_docs(docs):
    """MovieAgent с подменённым поиском по названию (без сети)."""
    agent = MovieAgent()
    agent.kinopoisk_client.search_movie_by_title = AsyncMock(
        return_value={'docs': docs}
    )
    return agent


def _api_doc(**extra):
    doc = {
        'id': 326,
        'name': 'Побег из Шоушенка',
        'year': 1994,
        'genres': [{'name': 'драма'}],
        'countries': [{'name': 'США'}],
        'rating': {'imdb': 9.3, 'kp': 9.1},
        'description': 'Описание',
        'poster': {'url': 'https://example.com/poster.jpg'},
        'type': 'movie',
    }
    doc.update(extra)
    return doc


def test_search_by_title_imdb_id_present():
    agent = _agent_with_docs([_api_doc(externalId={'imdb': 'tt0111161', 'tmdb': 278})])
    result = asyncio.run(agent.search_by_title(session=None, title='Побег из Шоушенка'))
    assert len(result) == 1
    assert result[0]['imdb_id'] == 'tt0111161'


def test_search_by_title_imdb_id_absent():
    """Без externalId в ответе API → imdb_id=None (незаметная деградация)."""
    agent = _agent_with_docs([_api_doc()])
    result = asyncio.run(agent.search_by_title(session=None, title='Побег из Шоушенка'))
    assert len(result) == 1
    assert result[0]['imdb_id'] is None


def test_search_by_title_existing_fields_untouched():
    """Регрессия: остальные поля карточки не изменились."""
    agent = _agent_with_docs([_api_doc(externalId={'imdb': 'tt0111161'})])
    result = asyncio.run(agent.search_by_title(session=None, title='Побег из Шоушенка'))
    card = result[0]
    assert card['id'] == 326
    assert card['title'] == 'Побег из Шоушенка'
    assert card['year'] == 1994
    assert card['rating_imdb'] == 9.3
    assert card['poster_url'] == 'https://example.com/poster.jpg'
