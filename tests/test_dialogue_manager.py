import asyncio
from unittest.mock import AsyncMock

from dialogue_manager import DialogueManager
from guardrails import MESSAGE_MAX_LENGTH, REFUSAL_TOO_LONG
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


def test_too_long_message_refused_without_llm():
    dm = _manager()
    dm.intent_classifier.classify_with_llm = AsyncMock()
    long_message = 'а' * (MESSAGE_MAX_LENGTH + 1)

    result = _run(dm.process_message(None, 'u1', long_message))

    assert result['response'] == REFUSAL_TOO_LONG
    assert result['needs_clarification'] is False
    dm.intent_classifier.classify_with_llm.assert_not_called()


# --- A3: проброс critics_approved из параметров в movie_agent ---

def test_general_request_passes_critics_approved():
    dm = _manager()
    dm.intent_classifier.classify_with_llm = AsyncMock(
        return_value={'intent': 'initial', 'genre': None, 'movie_type': 'movie',
                      'critics_approved': True}
    )
    dm.movie_agent.recommend_movies = AsyncMock(return_value=[])

    _run(dm.process_message(None, 'u1', 'фильмы одобренные критиками'))

    kwargs = dm.movie_agent.recommend_movies.call_args.kwargs
    assert kwargs['critics_approved'] is True


def test_general_request_critics_approved_default_false():
    """Регрессия: обычный запрос — critics_approved=False."""
    dm = _manager()
    dm.intent_classifier.classify_with_llm = AsyncMock(
        return_value={'intent': 'initial', 'genre': 'комедия', 'movie_type': 'movie'}
    )
    dm.movie_agent.recommend_movies = AsyncMock(return_value=[])

    _run(dm.process_message(None, 'u1', 'посоветуй комедию'))

    kwargs = dm.movie_agent.recommend_movies.call_args.kwargs
    assert kwargs['critics_approved'] is False


def _dm_with_session(intent_params, last_movies=None, last_params=None):
    """Менеджер с предзаполненной сессией и замокированным классификатором."""
    dm = _manager()
    session = dm.session_manager.get_session('u1')
    session.last_movies = last_movies or []
    session.last_params = last_params or {}
    dm.intent_classifier.classify_with_llm = AsyncMock(return_value=intent_params)
    dm.movie_agent.recommend_movies = AsyncMock(return_value=[])
    return dm


def test_refine_inherits_critics_mode_from_last_params():
    """Nit 4: «другие варианты» продолжают критический подбор из сессии."""
    dm = _dm_with_session(
        {'intent': 'alternative'},
        last_params={'genre': 'драма', 'movie_type': 'movie', 'critics_approved': True}
    )

    _run(dm.process_message(None, 'u1', 'другие варианты'))

    kwargs = dm.movie_agent.recommend_movies.call_args.kwargs
    assert kwargs['critics_approved'] is True


def test_refine_fresh_critics_mention_activates_mode():
    """Minor 2: свежее упоминание критиков в refine-запросе учитывается,
    даже если в сессии режима не было."""
    dm = _dm_with_session(
        {'intent': 'alternative', 'critics_approved': True},
        last_params={'genre': 'драма', 'movie_type': 'movie'}
    )

    _run(dm.process_message(None, 'u1', 'другие, одобренные критиками'))

    kwargs = dm.movie_agent.recommend_movies.call_args.kwargs
    assert kwargs['critics_approved'] is True


def test_refine_without_critics_stays_false():
    """Регрессия: refine без критиков в сессии и запросе — False."""
    dm = _dm_with_session(
        {'intent': 'alternative'},
        last_params={'genre': 'драма', 'movie_type': 'movie'}
    )

    _run(dm.process_message(None, 'u1', 'другие варианты'))

    kwargs = dm.movie_agent.recommend_movies.call_args.kwargs
    assert kwargs['critics_approved'] is False


def test_similar_does_not_inherit_critics_mode():
    """Minor 1: «похожие» берут режим ТОЛЬКО из текущего запроса —
    сессионный critics_approved не наследуется."""
    dm = _dm_with_session(
        {'intent': 'similar'},
        last_movies=[{'id': 1, 'title': 'Фильм', 'genre': 'драма', 'year': 2020}],
        last_params={'critics_approved': True, 'movie_type': 'movie'}
    )

    _run(dm.process_message(None, 'u1', 'похожие фильмы'))

    kwargs = dm.movie_agent.recommend_movies.call_args.kwargs
    assert kwargs['critics_approved'] is False


def test_similar_passes_critics_from_request():
    dm = _dm_with_session(
        {'intent': 'similar', 'critics_approved': True},
        last_movies=[{'id': 1, 'title': 'Фильм', 'genre': 'драма', 'year': 2020}],
        last_params={'movie_type': 'movie'}
    )

    _run(dm.process_message(None, 'u1', 'похожие, одобренные критиками'))

    kwargs = dm.movie_agent.recommend_movies.call_args.kwargs
    assert kwargs['critics_approved'] is True
