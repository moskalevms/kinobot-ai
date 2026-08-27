import time

from movie_agent import MovieAgent


def test_cache_keys_isolated_by_user():
    agent = MovieAgent()
    key_a = agent._get_cache_key('userA', 'комедия', None, None, None, None, None, 6.0, 8, 'movie', None)
    key_b = agent._get_cache_key('userB', 'комедия', None, None, None, None, None, 6.0, 8, 'movie', None)
    assert key_a != key_b


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
