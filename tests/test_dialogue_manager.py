import asyncio
from unittest.mock import AsyncMock

from dialogue_manager import DialogueManager
from session_manager import UserSession


class StubSessionManager:
    """In-memory заглушка менеджера сессий для тестов"""

    def __init__(self):
        self.sessions = {}
        self.saved = []

    def get_session(self, user_id):
        if user_id not in self.sessions:
            self.sessions[user_id] = UserSession(user_id)
        return self.sessions[user_id]

    def save_session(self, session):
        self.saved.append(session.user_id)

    def clear_session(self, user_id):
        self.sessions.pop(user_id, None)


def _manager():
    return DialogueManager(StubSessionManager())


def _run(coro):
    return asyncio.run(coro)


def test_info_without_target_movie_asks_clarification():
    dm = _manager()
    dm.intent_classifier.classify_with_llm = AsyncMock(
        return_value={'intent': 'info', 'target_movie': None}
    )
    result = _run(dm.process_message(None, 'u1', 'расскажи о фильме'))
    assert result['needs_clarification'] is True


def test_similar_without_history_asks_clarification():
    dm = _manager()
    dm.intent_classifier.classify_with_llm = AsyncMock(
        return_value={'intent': 'similar'}
    )
    result = _run(dm.process_message(None, 'u1', 'похожие фильмы'))
    assert result['needs_clarification'] is True


def test_alternative_without_params_asks_clarification():
    dm = _manager()
    dm.intent_classifier.classify_with_llm = AsyncMock(
        return_value={'intent': 'alternative'}
    )
    result = _run(dm.process_message(None, 'u1', 'другие варианты'))
    assert result['needs_clarification'] is True


def test_general_request_returns_movies_and_saves_session():
    dm = _manager()
    dm.intent_classifier.classify_with_llm = AsyncMock(
        return_value={'intent': 'initial', 'genre': 'комедия', 'movie_type': 'movie'}
    )
    movies = [{'id': 1, 'title': 'Фильм 1', 'year': 2020, 'rating': 8.0}]
    dm.movie_agent.recommend_movies = AsyncMock(return_value=movies)

    result = _run(dm.process_message(None, 'u1', 'посоветуй комедию'))

    assert result['needs_clarification'] is False
    assert result['movies_list'] == movies
    assert 'Фильм 1' in result['response']
    assert dm.session_manager.saved == ['u1']


def test_general_request_no_movies_reports_failure():
    dm = _manager()
    dm.intent_classifier.classify_with_llm = AsyncMock(
        return_value={'intent': 'initial', 'genre': 'комедия', 'movie_type': 'movie'}
    )
    dm.movie_agent.recommend_movies = AsyncMock(return_value=[])

    result = _run(dm.process_message(None, 'u1', 'посоветуй комедию'))

    assert result['needs_clarification'] is True
    assert 'не удалось найти' in result['response'].lower()


def test_info_request_returns_movie_card():
    dm = _manager()
    dm.intent_classifier.classify_with_llm = AsyncMock(
        return_value={'intent': 'info', 'target_movie': 'Начало'}
    )
    movie = {
        'id': 447301, 'title': 'Начало', 'year': 2010, 'genre': 'фантастика',
        'rating': 8.8, 'description': 'Сон внутри сна', 'poster_url': '',
    }
    dm.movie_agent.search_by_title = AsyncMock(return_value=[movie])

    result = _run(dm.process_message(None, 'u1', 'расскажи о фильме Начало'))

    assert result['needs_clarification'] is False
    assert result['movie'] == movie
    assert 'Начало' in result['response']
