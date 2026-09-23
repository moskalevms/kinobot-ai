"""Тесты A2/B3/A3/B6: итоговый словарь фильма и ранжирование в движке.

A2: `_format_movies_list` — `critics_rating`/`critics_votes` присутствуют
при наличии данных в API-ответе и равны None при отсутствии; семантика
«нет данных» согласована с A1. B6: внутренние поля ранжирования
(`weighted_score`/`critics_tier`/`country_priority`) и пересортировка
финала после обогащения RT (буст/демотивация, обратная совместимость без
rt, российская ветка и режим «одобрено критиками» не затронуты).
Все тесты без сети.
"""
import asyncio

import pytest

import recommendation_engine as engine_module
from kinopoisk_client import KinopoiskClient
from recommendation_engine import RecommendationEngine, _is_critics_approved


def _engine() -> RecommendationEngine:
    # Конструктор клиента не выполняет сетевых вызовов, ключ тестовый
    return RecommendationEngine(KinopoiskClient('test-kinopoisk-key'))


def _api_movie(imdb=7.5, kp=8.1, include_fc=True, fc=None, fc_votes=None):
    """Сырой словарь фильма в формате ответа kinopoisk.dev."""
    rating = {'imdb': imdb, 'kp': kp}
    votes = {'imdb': 100000, 'kp': 150000}
    if include_fc:
        rating['filmCritics'] = fc
        votes['filmCritics'] = fc_votes
    return {
        'id': 326,
        'name': 'Побег из Шоушенка',
        'year': 1994,
        'genres': [{'name': 'драма'}],
        'countries': [{'name': 'США'}],
        'rating': rating,
        'votes': votes,
        'description': 'Описание',
        'poster': {'url': 'https://example.com/poster.jpg'},
        'type': 'movie',
    }


def _format_one(movie):
    result = _engine()._format_movies_list([movie], limit=5)
    assert len(result) == 1
    return result[0]


def test_critics_fields_present():
    formatted = _format_one(_api_movie(fc=8.4, fc_votes=147))
    assert formatted['critics_rating'] == 8.4
    assert formatted['critics_votes'] == 147


def test_critics_fields_present_regardless_of_shrink_threshold():
    """Порог голосов для шринка (A1) на наличие полей не влияет."""
    formatted = _format_one(_api_movie(fc=9.0, fc_votes=3))
    assert formatted['critics_rating'] == 9.0
    assert formatted['critics_votes'] == 3


def test_critics_fields_absent_in_api_response():
    formatted = _format_one(_api_movie(include_fc=False))
    assert formatted['critics_rating'] is None
    assert formatted['critics_votes'] is None


def test_critics_fields_none_values():
    formatted = _format_one(_api_movie(fc=None, fc_votes=None))
    assert formatted['critics_rating'] is None
    assert formatted['critics_votes'] is None


def test_critics_zero_fc_means_no_data():
    """fc=0 (число или строка) — «нет данных»: critics_rating=None."""
    for zero in (0, 0.0, '0', '0.0'):
        formatted = _format_one(_api_movie(fc=zero, fc_votes=0))
        assert formatted['critics_rating'] is None
        # Голоса передаются как есть: 0 — значение, а не отсутствие
        assert formatted['critics_votes'] == 0


def test_critics_non_numeric_values_treated_as_no_data():
    formatted = _format_one(_api_movie(fc='не-число', fc_votes='много'))
    assert formatted['critics_rating'] is None
    assert formatted['critics_votes'] is None


def test_existing_fields_untouched():
    """Регрессия: существующие поля и формат выдачи не изменились."""
    formatted = _format_one(_api_movie(imdb=7.5, kp=8.1, fc=8.4, fc_votes=147))
    assert formatted['id'] == 326
    assert formatted['title'] == 'Побег из Шоушенка'
    assert formatted['year'] == 1994
    assert formatted['genre'] == 'драма'
    assert formatted['country'] == 'США'
    assert formatted['rating'] == 7.5          # иностранный фильм → IMDb
    assert formatted['rating_imdb'] == 7.5
    assert formatted['rating_kp'] == 8.1
    assert formatted['rating_source'] == 'IMDB'
    assert formatted['description'] == 'Описание'
    assert formatted['poster_url'] == 'https://example.com/poster.jpg'
    assert formatted['kinopoisk_url'] == 'https://www.kinopoisk.ru/film/326/'
    assert formatted['type'] == 'movie'


def test_limit_and_invalid_entries_still_work():
    """Регрессия: limit и отсечение некорректных записей не сломаны."""
    good = _api_movie(fc=8.4, fc_votes=147)
    bad = {'id': None, 'name': 'Без ID'}
    result = _engine()._format_movies_list([bad, good, good], limit=1)
    assert len(result) == 1
    assert result[0]['title'] == 'Побег из Шоушенка'


# --- B3: imdb_id в итоговом словаре фильма (join-ключ для OMDb/RT) ---


def test_imdb_id_present_from_external_id():
    """externalId.imdb в API-ответе → imdb_id в итоговом словаре."""
    movie = _api_movie()
    movie['externalId'] = {'imdb': 'tt0111161', 'tmdb': 278, 'trakt': None}
    formatted = _format_one(movie)
    assert formatted['imdb_id'] == 'tt0111161'


def test_imdb_id_absent_without_external_id():
    """Нет externalId (~31% базы) → imdb_id=None, фильм остаётся в выдаче."""
    formatted = _format_one(_api_movie())
    assert formatted['imdb_id'] is None


def test_imdb_id_none_when_imdb_null():
    movie = _api_movie()
    movie['externalId'] = {'imdb': None, 'tmdb': 123}
    assert _format_one(movie)['imdb_id'] is None


# --- A3: режим «одобрено критиками» (локальный порог и сортировка) ---

class _StubKinopoiskClient:
    """Заглушка клиента: записывает вызовы и возвращает заданные docs."""

    def __init__(self, docs):
        self.docs = docs
        self.search_calls = []
        self.top_calls = []

    async def search_movies(self, session, **kwargs):
        self.search_calls.append(kwargs)
        return {'docs': list(self.docs)}

    async def search_recommendation(self, session, **kwargs):
        self.top_calls.append(kwargs)
        return {'docs': []}


def _doc(doc_id, name, imdb, imdb_votes=100000, fc=None, fc_votes=None):
    """Сырой документ в формате ответа kinopoisk.dev."""
    return {
        'id': doc_id,
        'name': name,
        'year': 2020,
        'genres': [{'name': 'драма'}],
        'countries': [{'name': 'США'}],
        'rating': {'imdb': imdb, 'kp': None, 'filmCritics': fc},
        'votes': {'imdb': imdb_votes, 'kp': 0, 'filmCritics': fc_votes},
        'type': 'movie',
    }


def test_is_critics_approved_filters_garbage():
    """«10.0 при 1–2 рецензиях» отсеивается: голосов критиков < 10."""
    assert _is_critics_approved(_doc(1, 'Мусор', 7.0, fc=10.0, fc_votes=2)) is False


def test_is_critics_approved_passes_good_movie():
    assert _is_critics_approved(_doc(1, 'Хороший', 7.5, fc=7.5, fc_votes=50)) is True


def test_is_critics_approved_rejects_low_rating():
    """Локальный порог: fc < 7 отсеивается даже при многих голосах."""
    assert _is_critics_approved(_doc(1, 'Средний', 7.0, fc=6.0, fc_votes=100)) is False


def test_is_critics_approved_boundaries():
    """Границы: fc ровно 7.0 и votes ровно 10 — проходят (>=)."""
    assert _is_critics_approved(_doc(1, 'Граница', 7.0, fc=7.0, fc_votes=10)) is True
    assert _is_critics_approved(_doc(2, 'Ниже', 7.0, fc=6.9, fc_votes=10)) is False


def test_is_critics_approved_requires_data():
    assert _is_critics_approved(_doc(1, 'Без критиков', 8.5)) is False
    assert _is_critics_approved(_doc(2, 'Ноль', 8.5, fc=0, fc_votes=100)) is False


def test_engine_critics_mode_filters_and_sorts_by_fc():
    """Приёмка A3: выдача отсортирована по fc, мусор и fc<7 отсеяны."""
    docs = [
        _doc(1, 'Мусор', imdb=7.0, fc=10.0, fc_votes=2),      # голоса < 10
        _doc(2, 'Слабые критики', imdb=8.9, fc=7.2, fc_votes=50),
        _doc(3, 'Низкий fc', imdb=7.0, fc=6.0, fc_votes=100),  # fc < 7
        _doc(4, 'Топ критиков', imdb=8.0, fc=9.0, fc_votes=147),
        _doc(5, 'Без критиков', imdb=8.5),                     # нет данных
    ]
    client = _StubKinopoiskClient(docs)
    engine = RecommendationEngine(client)
    result = asyncio.run(engine.get_recommendations(
        session=None, critics_approved=True, min_imdb_rating=6.0
    ))
    # По взвешенной оценке «Слабые критики» (8.56) были бы выше
    # «Топ критиков» (8.5) — порядок доказывает сортировку по fc
    assert [m['title'] for m in result] == ['Топ критиков', 'Слабые критики']
    assert client.search_calls and client.search_calls[0]['critics_approved'] is True


def test_engine_critics_mode_skips_top250():
    """В режиме критиков top250 не запрашивается (был бы отсеян локально)."""
    client = _StubKinopoiskClient([_doc(1, 'Топ критиков', 8.0, fc=9.0, fc_votes=147)])
    engine = RecommendationEngine(client)
    asyncio.run(engine.get_recommendations(
        session=None, critics_approved=True, is_top=True, min_imdb_rating=6.0
    ))
    assert client.top_calls == []


def test_engine_default_mode_untouched():
    """Регрессия: без critics_approved фильмы без fc не отсеиваются."""
    docs = [
        _doc(1, 'Без критиков', imdb=8.5),
        _doc(2, 'С критиками', imdb=7.5, fc=7.5, fc_votes=50),
    ]
    client = _StubKinopoiskClient(docs)
    engine = RecommendationEngine(client)
    result = asyncio.run(engine.get_recommendations(session=None, min_imdb_rating=6.0))
    assert client.search_calls[0]['critics_approved'] is False
    # Сортировка по взвешенной оценке: 8.5 > 7.5
    assert [m['title'] for m in result] == ['Без критиков', 'С критиками']
    assert client.top_calls == []


# --- B6: RT в ранжировании (пересортировка финала после обогащения) ---


def _fake_enrich_with(rt_map):
    """Фейк enrich_movies_with_rt_scores: проставляет rt_score по названию.

    Имитирует результат B5 (поля присутствуют всегда), не обращаясь к OMDb;
    порядок фильмов не меняет — как и настоящий пайплайн обогащения.
    """
    async def _enrich(session, movies, client=None):
        for movie in movies:
            movie['rt_score'] = rt_map.get(movie['title'])
            movie['metascore'] = None
        return movies
    return _enrich


def test_format_includes_ranking_fields():
    """D4: `_format_movies_list` кладёт s1, ступень fc и страновой приоритет."""
    movie = _api_movie(imdb=7.5, kp=8.1, fc=8.4, fc_votes=147)
    formatted = _format_one(movie)
    # s1: 7.5 + 0.2·(8.4 − 7.5) = 7.68, ступень fc +0.3 → 7.98
    assert formatted['weighted_score'] == pytest.approx(7.98)
    assert formatted['critics_tier'] == pytest.approx(0.3)
    assert formatted['country_priority'] == 0  # США


def test_format_russian_search_uses_kp_branch():
    """В российской ветке s1 считается по КП (rt дальше не применяется)."""
    formatted = _engine()._format_movies_list(
        [_api_movie(imdb=7.5, kp=8.1)], limit=5, is_russian_search=True
    )
    assert formatted[0]['weighted_score'] == pytest.approx(8.1)


def test_engine_reranks_final_list_with_rt(monkeypatch):
    """Приёмка B6: rt=91 поднимает фильм 7.4 (→8.04) выше 7.6 без rt."""
    monkeypatch.setattr(
        engine_module, 'enrich_movies_with_rt_scores',
        _fake_enrich_with({'С rt': 91}),
    )
    docs = [
        _doc(1, 'Без rt', imdb=7.6),
        _doc(2, 'С rt', imdb=7.4),
    ]
    engine = RecommendationEngine(_StubKinopoiskClient(docs))
    result = asyncio.run(engine.get_recommendations(session=None, min_imdb_rating=6.0))
    assert [m['title'] for m in result] == ['С rt', 'Без rt']
    # s2 победителя: 7.4 + 0.2·(9.1 − 7.4) = 7.74, ступень +0.3 → 8.04
    assert result[0]['weighted_score'] == pytest.approx(8.04)


def test_engine_demotes_final_movie_with_low_rt(monkeypatch):
    """Демотивация: rt=20 опускает фильм 8.0 (→6.5) ниже 7.0 без rt."""
    monkeypatch.setattr(
        engine_module, 'enrich_movies_with_rt_scores',
        _fake_enrich_with({'Провал': 20}),
    )
    docs = [
        _doc(1, 'Провал', imdb=8.0),
        _doc(2, 'Середняк', imdb=7.0),
    ]
    engine = RecommendationEngine(_StubKinopoiskClient(docs))
    result = asyncio.run(engine.get_recommendations(session=None, min_imdb_rating=6.0))
    assert [m['title'] for m in result] == ['Середняк', 'Провал']


def test_engine_order_unchanged_without_rt(monkeypatch):
    """Обратная совместимость: флаг Epic B выключен → порядок фазы 0+A1+B5."""
    monkeypatch.setenv('ENABLE_RT_SCORES', 'false')
    docs = [
        _doc(1, 'Первый', imdb=7.6),
        _doc(2, 'Второй', imdb=7.4),
    ]
    engine = RecommendationEngine(_StubKinopoiskClient(docs))
    result = asyncio.run(engine.get_recommendations(session=None, min_imdb_rating=6.0))
    # Настоящий пайплайн B5 проставит rt_score=None — пересортировка no-op
    assert [m['title'] for m in result] == ['Первый', 'Второй']
    assert all(m['rt_score'] is None for m in result)


def test_engine_russian_search_unaffected_by_rt(monkeypatch):
    """Российская ветка не затронута даже при высоких rt."""
    monkeypatch.setattr(
        engine_module, 'enrich_movies_with_rt_scores',
        _fake_enrich_with({'Ниже': 95}),
    )

    def _ru_doc(doc_id, name, kp):
        return {
            'id': doc_id, 'name': name, 'year': 2020,
            'genres': [{'name': 'драма'}], 'countries': [{'name': 'Россия'}],
            'rating': {'imdb': None, 'kp': kp},
            'votes': {'imdb': 0, 'kp': 100000},
            'type': 'movie',
        }

    docs = [_ru_doc(1, 'Выше', 8.0), _ru_doc(2, 'Ниже', 7.0)]
    engine = RecommendationEngine(_StubKinopoiskClient(docs))
    result = asyncio.run(engine.get_recommendations(
        session=None, country='россия', min_imdb_rating=6.0
    ))
    assert [m['title'] for m in result] == ['Выше', 'Ниже']
    # s1 российской ветки не пересчитывается: rt применён не был
    assert result[1]['weighted_score'] == pytest.approx(7.0)


def test_engine_critics_mode_keeps_fc_order_with_rt(monkeypatch):
    """Режим «одобрено критиками»: порядок по убыванию fc, RT не мешает."""
    monkeypatch.setattr(
        engine_module, 'enrich_movies_with_rt_scores',
        _fake_enrich_with({'Слабые критики': 91}),
    )
    docs = [
        _doc(2, 'Слабые критики', imdb=8.9, fc=7.2, fc_votes=50),
        _doc(4, 'Топ критиков', imdb=8.0, fc=9.0, fc_votes=147),
    ]
    engine = RecommendationEngine(_StubKinopoiskClient(docs))
    result = asyncio.run(engine.get_recommendations(
        session=None, critics_approved=True, min_imdb_rating=6.0
    ))
    # Без пропуска пересортировки «Слабые критики» (s2 ≈ 8.97) обошли бы
    # «Топ критиков» (s1 = 8.5) — порядок по fc доказывает пропуск rerank
    assert [m['title'] for m in result] == ['Топ критиков', 'Слабые критики']
