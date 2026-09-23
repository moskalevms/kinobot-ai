"""Сквозная верификация фазы 1 Epic B (B8, verify-rt-phase1).

Критерий приёмки B8: при выключенном feature flag (`ENABLE_RT_SCORES=false`)
поведение системы совпадает с фазой 0 — выдача идентична по составу,
порядку и пользовательскому тексту, RT-поля присутствуют, но не значимы
(None), бейдж «🍅» не выводится, клиент OMDb не создаётся (сети и БД нет).

Эталон «фазы 0» моделируется прогоном того же движка с обогащением,
заменённым тождественной функцией (RT-поля не добавляются, пересортировки
нет): именно так пайплайн работал до B5/B6. Все тесты без сети.
"""
import asyncio
from typing import Any, Dict, List

import recommendation_engine as engine_module
import rt_enrichment
from dialogue_manager import DialogueManager, format_movie_card
from recommendation_engine import RecommendationEngine
from rt_enrichment import enrich_movies_with_rt_scores
from session_manager import UserSession


class _StubSessionManager:
    """In-memory заглушка менеджера сессий (без БД)."""

    def __init__(self):
        self.sessions = {}

    def get_session(self, user_id):
        if user_id not in self.sessions:
            self.sessions[user_id] = UserSession(user_id)
        return self.sessions[user_id]

    def save_session(self, session):
        return None

    def clear_session(self, user_id):
        self.sessions.pop(user_id, None)


class _StubKinopoiskClient:
    """Заглушка клиента Кинопоиска по образцу tests/test_recommendation_engine.py."""

    def __init__(self, docs: List[Dict[str, Any]]):
        self.docs = docs
        self.search_calls: List[Dict[str, Any]] = []

    async def search_movies(self, session: Any, **kwargs: Any) -> Dict[str, Any]:
        self.search_calls.append(kwargs)
        return {'docs': list(self.docs)}

    async def search_recommendation(self, session: Any, **kwargs: Any) -> Dict[str, Any]:
        return {'docs': []}


def _doc(doc_id: int, name: str, imdb: float) -> Dict[str, Any]:
    """Сырой документ kinopoisk.dev, проходящий filter_movies_by_quality."""
    return {
        'id': doc_id,
        'name': name,
        'year': 2020,
        'genres': [{'name': 'драма'}],
        'countries': [{'name': 'США'}],
        'rating': {'imdb': imdb, 'kp': None, 'filmCritics': None},
        'votes': {'imdb': 100000, 'kp': 0, 'filmCritics': None},
        'externalId': {'imdb': f'tt{doc_id:07d}'},
        'type': 'movie',
    }


def _run_engine(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    engine = RecommendationEngine(_StubKinopoiskClient(docs))
    return asyncio.run(engine.get_recommendations(session=None, min_imdb_rating=6.0))


def _strip_rt_fields(movies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Копии фильмов без RT-ключей — вид словарей в фазе 0."""
    return [
        {k: v for k, v in movie.items() if k not in ('rt_score', 'metascore')}
        for movie in movies
    ]


def test_flag_off_result_identical_to_phase0(monkeypatch):
    """Сквозной паритет: flag off ≡ фаза 0 + инертные RT-поля (None).

    Прогон 1 — модель фазы 0: обогащение заменено тождественной функцией
    (поля rt_score/metascore не добавляются, пересортировка B6 не применяется).
    Прогон 2 — flag off: настоящий пайплайн B5/B6 без сети и БД.
    Результат flag off обязан совпадать с фазой 0 по составу, порядку и всем
    полям; единственное отличие — ключи rt_score/metascore со значением None.
    """
    docs = [_doc(1, 'Первый', 7.6), _doc(2, 'Второй', 7.4)]

    # --- Прогон 1: модель фазы 0 (обогащение — тождественная функция) ---
    async def _identity_enrich(session, movies, client=None):
        return movies

    monkeypatch.setattr(
        engine_module, 'enrich_movies_with_rt_scores', _identity_enrich
    )
    phase0 = _run_engine(docs)
    assert 'rt_score' not in phase0[0] and 'metascore' not in phase0[0]

    # --- Прогон 2: flag off, настоящий пайплайн B5/B6 ---
    monkeypatch.setattr(
        engine_module, 'enrich_movies_with_rt_scores', enrich_movies_with_rt_scores
    )
    monkeypatch.setenv('ENABLE_RT_SCORES', 'false')
    # «Бомба»: создание клиента OMDb при выключенном флаге недопустимо.
    # След вызова фиксируется ДО raise — контур fail-silent проглатывает
    # AssertionError, доказательством служит пустой список client_creations.
    client_creations: List[str] = []

    def _sentinel_make_client():
        client_creations.append('make_client')
        raise AssertionError('клиент OMDb создан при выключенном флаге')

    monkeypatch.setattr(rt_enrichment, '_make_client', _sentinel_make_client)
    flag_off = _run_engine(docs)

    assert client_creations == []
    # Состав и порядок — фаза 0 (сортировка s1, без RT-пересортировки)
    assert [m['title'] for m in flag_off] == [m['title'] for m in phase0]
    assert [m['title'] for m in flag_off] == ['Первый', 'Второй']
    # Все поля, кроме инертных RT-ключей, совпадают с фазой 0 побайтово
    assert _strip_rt_fields(flag_off) == phase0
    assert all(
        m['rt_score'] is None and m['metascore'] is None for m in flag_off
    )


def test_flag_off_user_text_matches_phase0(monkeypatch):
    """Пользовательский текст при flag off не содержит следов RT (B7/B8).

    Текст списка (`_generate_list_response`), inline-клавиатура и карточка
    (`format_movie_card`) для выдачи flag off побайтово равны тексту для тех
    же фильмов без RT-ключей (фаза 0); бейдж «🍅» отсутствует.
    """
    monkeypatch.setenv('ENABLE_RT_SCORES', 'false')
    flag_off = _run_engine([_doc(1, 'Первый', 7.6), _doc(2, 'Второй', 7.4)])
    stripped = _strip_rt_fields(flag_off)

    dm = DialogueManager(_StubSessionManager())
    text_off, kb_off = dm._generate_list_response(
        [dict(m) for m in flag_off], 'Рекомендации:'
    )
    text_p0, kb_p0 = dm._generate_list_response(
        [dict(m) for m in stripped], 'Рекомендации:'
    )
    assert text_off == text_p0
    assert kb_off.to_dict() == kb_p0.to_dict()
    assert '🍅' not in text_off

    card_off = format_movie_card(dict(flag_off[0]))
    card_p0 = format_movie_card(dict(stripped[0]))
    assert card_off == card_p0
    assert '🍅' not in card_off
