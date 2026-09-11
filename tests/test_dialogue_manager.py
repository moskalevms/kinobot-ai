import asyncio
from unittest.mock import AsyncMock

import pytest

from dialogue_manager import (
    DialogueManager,
    format_movie_card,
    format_rt_badge,
    rt_badge_suffix,
)
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


# --- B7: бейдж Tomatometer «🍅 91%» (изменение add-rt-badge) ---

def _movie(**overrides):
    """Словарь фильма финальной выдачи (поля обогащения B5 включены)."""
    movie = {
        'id': 447301, 'title': 'Начало', 'year': 2010, 'genre': 'фантастика',
        'rating': 8.8, 'description': 'Сон внутри сна', 'poster_url': '',
        'rt_score': None, 'metascore': None,
    }
    movie.update(overrides)
    return movie


@pytest.mark.parametrize('rt_score,expected', [
    (91, '🍅 91%'),
    (7, '🍅 7%'),
    (100, '🍅 100%'),
    (0, '🍅 0%'),  # 0% одобрения — валидные данные, не «нет оценки»
])
def test_format_rt_badge_valid_scores(rt_score, expected):
    """Формат бейджа: эмодзи, пробел, целый процент, знак процента."""
    assert format_rt_badge(_movie(rt_score=rt_score)) == expected


@pytest.mark.parametrize('rt_score', [
    None,        # нет данных (нет IMDb ID, сбой OMDb, флаг Epic B выключен)
    '91',        # строка вместо числа
    91.0,        # дробное: бейдж обязан быть целым процентом
    True,        # bool — не оценка
    101,         # вне шкалы Tomatometer
    -5,          # вне шкалы Tomatometer
    'N/A',       # мусорные данные
])
def test_format_rt_badge_absent_for_invalid_scores(rt_score):
    """Без валидного rt_score бейджа нет (пустая строка)."""
    assert format_rt_badge(_movie(rt_score=rt_score)) == ''


def test_format_rt_badge_without_key():
    """Поля rt_score может не быть вовсе (старый кэш/другой путь выдачи)."""
    movie = _movie()
    del movie['rt_score']
    assert format_rt_badge(movie) == ''


def test_format_rt_badge_ignores_metascore():
    """Только Metacritic: бейдж Tomatometer не выводится."""
    assert format_rt_badge(_movie(metascore=82)) == ''


def test_rt_badge_suffix_with_score():
    assert rt_badge_suffix(_movie(rt_score=91)) == ' · 🍅 91%'


def test_rt_badge_suffix_without_score_is_empty():
    """Пустой суффикс — никаких висящих разделителей и пробелов."""
    assert rt_badge_suffix(_movie()) == ''


def test_movie_card_contains_badge():
    card = format_movie_card(_movie(rt_score=91))

    assert 'с рейтингом 8.8 · 🍅 91%.' in card
    assert card.startswith('🎬 <strong>Начало</strong> (2010) — фантастика')
    assert card.endswith('Сон внутри сна')


def test_movie_card_without_badge_is_backward_compatible():
    """Без rt_score текст карточки побайтово прежний (до B7)."""
    assert format_movie_card(_movie()) == (
        '🎬 <strong>Начало</strong> (2010) — фантастика с рейтингом 8.8.\n'
        'Сон внутри сна'
    )


def test_movie_card_escapes_html():
    """Экранирование полей карточки сохранено после рефакторинга."""
    card = format_movie_card(_movie(title='Фильм <b>&</b>', rt_score=76))

    assert '&lt;b&gt;&amp;&lt;/b&gt;' in card
    assert '<b>&</b>' not in card
    assert ' · 🍅 76%.' in card


def test_info_response_contains_badge():
    dm = _manager()
    dm.intent_classifier.classify_with_llm = AsyncMock(
        return_value={'intent': 'info', 'target_movie': 'Начало'}
    )
    dm.movie_agent.search_by_title = AsyncMock(return_value=[_movie(rt_score=91)])

    result = _run(dm.process_message(None, 'u1', 'расскажи о фильме Начало'))

    assert '🍅 91%' in result['response']


def test_info_response_without_rt_score_has_no_badge():
    dm = _manager()
    dm.intent_classifier.classify_with_llm = AsyncMock(
        return_value={'intent': 'info', 'target_movie': 'Начало'}
    )
    dm.movie_agent.search_by_title = AsyncMock(return_value=[_movie()])

    result = _run(dm.process_message(None, 'u1', 'расскажи о фильме Начало'))

    assert '🍅' not in result['response']
    assert ' · ' not in result['response']


def test_list_response_line_with_badge():
    dm = _manager()
    movies = [_movie(rt_score=91)]

    response, keyboard = dm._generate_list_response(movies, 'Рекомендации фильма')

    assert (
        '1. <strong>Начало</strong> (2010) — ⭐ 8.8 · 🍅 91%\n' in response
    )
    # Бейдж — в конце строки, перенос строки сразу после него
    assert '🍅 91%\n' in response
    assert len(keyboard.inline_keyboard) == 1


def test_list_response_mixed_badges():
    """Бейдж только у фильмов с данными: строки без rt_score не меняются."""
    dm = _manager()
    movies = [
        _movie(rt_score=91),
        _movie(id=2, title='Фильм 2', year=2020, rating=8.0),
    ]

    response, keyboard = dm._generate_list_response(movies, 'Заголовок')

    lines = response.splitlines()
    assert lines[1] == '1. <strong>Начало</strong> (2010) — ⭐ 8.8 · 🍅 91%'
    assert lines[2] == '2. <strong>Фильм 2</strong> (2020) — ⭐ 8.0'
    assert response.count('🍅') == 1
    assert len(keyboard.inline_keyboard) == 2


def test_list_response_without_rt_scores_is_backward_compatible():
    """Выдача без RT-данных побайтово совпадает с прежним форматом."""
    dm = _manager()
    movies = [{'id': 1, 'title': 'Фильм 1', 'year': 2020, 'rating': 8.0}]

    response, keyboard = dm._generate_list_response(movies, 'Заголовок')

    assert response == (
        '<strong>Заголовок</strong>\n'
        '1. <strong>Фильм 1</strong> (2020) — ⭐ 8.0\n'
    )
    assert '🍅' not in response
    assert ' · ' not in response
    # Кнопки не меняются: бейдж в подпись кнопки не добавляется
    assert keyboard.inline_keyboard[0][0].text == 'Подробнее: Фильм 1'


def test_list_response_badge_not_in_buttons():
    dm = _manager()
    movies = [_movie(rt_score=91)]

    _, keyboard = dm._generate_list_response(movies, 'Заголовок')

    assert '🍅' not in keyboard.inline_keyboard[0][0].text
    assert keyboard.inline_keyboard[0][0].callback_data == 'info:447301'
