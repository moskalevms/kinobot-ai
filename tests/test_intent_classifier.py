import os

from intent_classifier import IntentClassifier

PROMPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'src', 'prompts'
)


def _classifier():
    return IntentClassifier(llm_router=None, prompts_dir=PROMPTS_DIR)


def test_explicit_year_range():
    params = _classifier()._classify_fallback('лучшие фильмы 2024-2025')
    assert params['year_range'] == (2024, 2025)
    assert params['year'] is None


def test_decade_textual():
    params = _classifier()._classify_fallback('драмы девяностых')
    assert params['year'] == 1990


def test_top_count_and_min_rating():
    params = _classifier()._classify_fallback('топ 50 фильмов')
    assert params['count'] == 50
    assert params['min_rating'] == 7.0


def test_country_extraction():
    params = _classifier()._classify_fallback('французские драмы')
    assert params['country'] == 'Франция'


def test_genre_extraction():
    params = _classifier()._classify_fallback('посоветуй комедию')
    assert params['genre'] == 'комедия'


def test_series_type():
    params = _classifier()._classify_fallback('посоветуй сериалы')
    assert params['movie_type'] == 'tv-series'


def test_movie_type_default():
    params = _classifier()._classify_fallback('посоветуй комедию')
    assert params['movie_type'] == 'movie'


def test_info_intent():
    params = _classifier()._classify_fallback('расскажи о фильме Начало')
    assert params['intent'] == 'info'


def test_default_intent_initial():
    params = _classifier()._classify_fallback('посоветуй комедию')
    assert params['intent'] == 'initial'
