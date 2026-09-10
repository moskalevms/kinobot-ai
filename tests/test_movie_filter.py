import pytest

from utils.movie_filter import (
    extract_critics_fields,
    extract_imdb_id,
    filter_movies_by_quality,
    get_country_priority,
    get_weighted_rating,
    is_russian_content,
    should_exclude_by_genre,
)


def _movie(name, genres, countries, imdb=None, kp=None, imdb_votes=0, kp_votes=0,
           fc=None, fc_votes=0):
    """Фильм-фикстура; fc/fc_votes — рейтинг и голоса кинокритиков."""
    return {
        'name': name,
        'genres': [{'name': g} for g in genres],
        'countries': [{'name': c} for c in countries],
        'rating': {'imdb': imdb, 'kp': kp, 'filmCritics': fc},
        'votes': {'imdb': imdb_votes, 'kp': kp_votes, 'filmCritics': fc_votes},
    }


def test_excluded_genre_filtered():
    movie = _movie('Концерт', ['концерт'], ['США'])
    assert should_exclude_by_genre(movie)


def test_excluded_genre_allowed_explicitly():
    movie = _movie('Концерт', ['концерт'], ['США'])
    assert not should_exclude_by_genre(movie, {'концерт'})


def test_is_russian_content():
    assert is_russian_content(_movie('Фильм', [], ['Россия']))
    assert is_russian_content(_movie('Фильм', [], ['СССР']))
    assert not is_russian_content(_movie('Фильм', [], ['США']))


def test_weighted_rating_prefers_kp_for_russian_content():
    movie = _movie('Фильм', [], ['Россия'], imdb=7.0, kp=8.0)
    assert get_weighted_rating(movie) == 8.0


def test_weighted_rating_prefers_imdb_for_foreign_content():
    movie = _movie('Фильм', [], ['США'], imdb=7.0, kp=8.0)
    assert get_weighted_rating(movie) == 7.0


# --- A1: учёт рейтинга кинокритиков (rating.filmCritics) ---

def test_critics_zero_fc_means_no_data():
    """fc == 0 — «нет данных», НЕ провальная оценка: поведение как без fc."""
    movie = _movie('Фильм', [], ['США'], imdb=7.0, fc=0, fc_votes=100)
    assert get_weighted_rating(movie) == 7.0


def test_critics_none_fc_means_no_data():
    movie = _movie('Фильм', [], ['США'], imdb=7.0, fc=None, fc_votes=50)
    assert get_weighted_rating(movie) == 7.0


def test_critics_missing_fields_backward_compatible():
    """Словарь вообще без полей filmCritics — ровно прежнее поведение."""
    movie = {
        'name': 'Фильм',
        'countries': [{'name': 'США'}],
        'rating': {'imdb': 7.0, 'kp': None},
        'votes': {'imdb': 1000, 'kp': 0},
    }
    assert get_weighted_rating(movie) == 7.0


def test_critics_fallbacks_unchanged_without_fc():
    """Фолбэки прежней формулы не изменились (КП*0.8, IMDb*0.9, 0.0)."""
    foreign_kp_only = _movie('Фильм', [], ['США'], kp=8.0)
    assert get_weighted_rating(foreign_kp_only) == pytest.approx(6.4)

    russian_imdb_only = _movie('Фильм', [], ['Россия'], imdb=7.0)
    assert get_weighted_rating(russian_imdb_only) == pytest.approx(6.3)

    no_ratings = _movie('Фильм', [], ['США'])
    assert get_weighted_rating(no_ratings) == 0.0


def test_critics_no_shrink_below_min_votes():
    """votes.filmCritics < 10 — шринк не применяется даже при высоком fc."""
    movie = _movie('Фильм', [], ['США'], imdb=7.0, fc=9.0, fc_votes=9)
    assert get_weighted_rating(movie) == 7.0


def test_critics_shrink_applied_at_min_votes():
    """Граница: votes.filmCritics == 10 — шринк применяется."""
    movie = _movie('Фильм', [], ['США'], imdb=7.0, fc=9.0, fc_votes=10)
    # s1 = 7.0 + 0.2 * (9.0 - 7.0) = 7.4; ступени нет (голосов < 20)
    assert get_weighted_rating(movie) == pytest.approx(7.4)


def test_critics_shrink_toward_higher_consensus():
    movie = _movie('Фильм', [], ['США'], imdb=7.0, fc=8.5, fc_votes=15)
    # s1 = 7.0 + 0.2 * (8.5 - 7.0) = 7.3
    assert get_weighted_rating(movie) == pytest.approx(7.3)


def test_critics_certified_fresh_tier():
    """Ступень +0.3: fc >= 8 и votes.filmCritics >= 20."""
    movie = _movie('Фильм', [], ['США'], imdb=7.0, fc=8.4, fc_votes=147)
    # s1 = 7.0 + 0.2 * 1.4 = 7.28; +0.3 → 7.58
    assert get_weighted_rating(movie) == pytest.approx(7.58)


def test_critics_critical_flop_tier():
    """Ступень −0.3: fc <= 3 и votes.filmCritics >= 20."""
    movie = _movie('Фильм', [], ['США'], imdb=7.0, fc=2.5, fc_votes=30)
    # s1 = 7.0 + 0.2 * (2.5 - 7.0) = 6.1; −0.3 → 5.8
    assert get_weighted_rating(movie) == pytest.approx(5.8)


def test_critics_high_fc_low_votes_no_tier():
    """fc высокий, но голосов 10–19: шринк есть, ступени +0.3 нет."""
    movie = _movie('Фильм', [], ['США'], imdb=7.0, fc=9.5, fc_votes=19)
    # s1 = 7.0 + 0.2 * 2.5 = 7.5; tier = 0
    assert get_weighted_rating(movie) == pytest.approx(7.5)


def test_critics_low_fc_low_votes_no_penalty():
    """fc низкий, но голосов < 20: шринк мягкий, ступени −0.3 нет."""
    movie = _movie('Фильм', [], ['США'], imdb=7.0, fc=2.0, fc_votes=12)
    # s1 = 7.0 + 0.2 * (2.0 - 7.0) = 6.0; tier = 0
    assert get_weighted_rating(movie) == pytest.approx(6.0)


def test_critics_score_clamped_to_zero():
    """Кламп снизу: score не опускается ниже 0."""
    movie = _movie('Фильм', [], ['США'], imdb=0.1, fc=0.5, fc_votes=30)
    # s1 = 0.1 + 0.2 * 0.4 = 0.18; −0.3 → −0.12 → 0.0
    assert get_weighted_rating(movie) == 0.0


def test_critics_score_clamped_to_ten():
    """Кламп сверху: score не поднимается выше 10."""
    movie = _movie('Фильм', [], ['США'], imdb=10.0, fc=10.0, fc_votes=30)
    # s1 = 10.0; +0.3 → 10.3 → 10.0
    assert get_weighted_rating(movie) == 10.0


def test_critics_applied_in_russian_branch():
    """Российская ветка (base = КП) тоже учитывает fc."""
    movie = _movie('Фильм', [], ['Россия'], kp=8.0, fc=9.0, fc_votes=30)
    # base = 8.0; s1 = 8.0 + 0.2 * 1.0 = 8.2; +0.3 → 8.5
    assert get_weighted_rating(movie) == pytest.approx(8.5)


def test_critics_applied_with_russian_search_flag():
    movie = _movie('Фильм', [], ['США'], imdb=7.0, kp=8.0, fc=6.0, fc_votes=50)
    # base = 8.0 (КП); s1 = 8.0 + 0.2 * (6.0 - 8.0) = 7.6; tier = 0
    assert get_weighted_rating(movie, is_russian_search=True) == pytest.approx(7.6)


def test_critics_non_numeric_fc_treated_as_no_data():
    """Нечисловой fc — «нет данных», без падений."""
    movie = _movie('Фильм', [], ['США'], imdb=7.0)
    movie['rating']['filmCritics'] = 'не-число'
    movie['votes']['filmCritics'] = 100
    assert get_weighted_rating(movie) == 7.0


def test_critics_string_zero_fc_means_no_data():
    """Регрессия M1: строковый fc='0'/'0.0' — «нет данных», НЕ демотивация.

    Сравнение строки с нулём ('0' == 0) даёт False, поэтому ноль
    проверяется только после приведения к float.
    """
    for zero in ('0', '0.0'):
        movie = _movie('Фильм', [], ['США'], imdb=7.0)
        movie['rating']['filmCritics'] = zero
        movie['votes']['filmCritics'] = 100
        assert get_weighted_rating(movie) == 7.0


def test_critics_non_numeric_fc_votes_treated_as_no_data():
    """Нечисловой votes.filmCritics — «нет данных», без падений."""
    movie = _movie('Фильм', [], ['США'], imdb=7.0, fc=9.0)
    movie['votes']['filmCritics'] = 'много'
    assert get_weighted_rating(movie) == 7.0


def test_critics_no_audience_ratings_stays_zero():
    """Регрессия m2: нет зрительских рейтингов → оценка остаётся 0.0,
    коррекция критиков не порождает оценку из одних критиков."""
    movie = _movie('Фильм', [], ['США'], imdb=None, kp=None, fc=9.0, fc_votes=100)
    assert get_weighted_rating(movie) == 0.0


def test_critics_tier_boundary_fc_exactly_eight():
    """Граница ступени: fc ровно 8.0 при votes >= 20 даёт +0.3 (>=)."""
    movie = _movie('Фильм', [], ['США'], imdb=7.0, fc=8.0, fc_votes=20)
    # s1 = 7.0 + 0.2 * 1.0 = 7.2; +0.3 → 7.5
    assert get_weighted_rating(movie) == pytest.approx(7.5)


def test_critics_tier_boundary_fc_exactly_three():
    """Граница ступени: fc ровно 3.0 при votes >= 20 даёт −0.3 (<=)."""
    movie = _movie('Фильм', [], ['США'], imdb=7.0, fc=3.0, fc_votes=20)
    # s1 = 7.0 + 0.2 * (3.0 - 7.0) = 6.2; −0.3 → 5.9
    assert get_weighted_rating(movie) == pytest.approx(5.9)


def test_critics_tier_boundary_votes_exactly_twenty():
    """Граница ступени: votes.filmCritics ровно 20 достаточно для tier."""
    movie = _movie('Фильм', [], ['США'], imdb=7.0, fc=9.0, fc_votes=20)
    # s1 = 7.0 + 0.2 * 2.0 = 7.4; +0.3 → 7.7
    assert get_weighted_rating(movie) == pytest.approx(7.7)


# --- A2: извлечение полей критиков (extract_critics_fields) ---

def test_extract_critics_fields_present():
    movie = _movie('Фильм', [], ['США'], imdb=7.0, fc=8.4, fc_votes=147)
    assert extract_critics_fields(movie) == (8.4, 147)


def test_extract_critics_fields_no_votes_gate():
    """В отличие от A1, порог голосов для шринка не применяется."""
    movie = _movie('Фильм', [], ['США'], imdb=7.0, fc=9.0, fc_votes=3)
    assert extract_critics_fields(movie) == (9.0, 3)


def test_extract_critics_fields_no_data():
    """fc отсутствует/None/0/строковый '0'/нечисловой → None."""
    assert extract_critics_fields(_movie('Ф', [], [])) == (None, 0)
    assert extract_critics_fields(_movie('Ф', [], [], fc=None)) == (None, 0)
    assert extract_critics_fields(_movie('Ф', [], [], fc=0, fc_votes=50)) == (None, 50)
    movie_str = _movie('Ф', [], [])
    movie_str['rating']['filmCritics'] = '0.0'
    assert extract_critics_fields(movie_str) == (None, 0)
    movie_bad = _movie('Ф', [], [])
    movie_bad['rating']['filmCritics'] = 'не-число'
    movie_bad['votes']['filmCritics'] = 'много'
    assert extract_critics_fields(movie_bad) == (None, None)


def test_critics_boost_passes_min_rating_filter():
    """Интеграция: шринк + ступень поднимают фильм выше min_rating."""
    movie = _movie('Фильм', ['драма'], ['США'], imdb=5.9, imdb_votes=100000,
                   fc=9.0, fc_votes=100)
    # s1 = 5.9 + 0.2 * 3.1 = 6.52; +0.3 → 6.82 ≥ 6.0
    result = filter_movies_by_quality([movie], min_rating=6.0)
    assert [m['name'] for m in result] == ['Фильм']


def test_critics_penalty_fails_min_rating_filter():
    """Интеграция: критический провал опускает фильм ниже min_rating."""
    movie = _movie('Фильм', ['драма'], ['США'], imdb=6.1, imdb_votes=100000,
                   fc=2.0, fc_votes=30)
    # s1 = 6.1 + 0.2 * (2.0 - 6.1) = 5.28; −0.3 → 4.98 < 6.0
    result = filter_movies_by_quality([movie], min_rating=6.0)
    assert result == []


def test_critics_reorder_by_consensus():
    """Интеграция: сортировка учитывает ступень «одобрено критиками»."""
    plain = _movie('Обычный', ['драма'], ['США'], imdb=7.5, imdb_votes=100000)
    fresh = _movie('Одобренный', ['драма'], ['США'], imdb=7.4, imdb_votes=100000,
                   fc=9.0, fc_votes=50)
    # plain: 7.5; fresh: 7.4 + 0.2 * 1.6 + 0.3 = 8.02
    result = filter_movies_by_quality([plain, fresh], min_rating=6.0)
    assert [m['name'] for m in result] == ['Одобренный', 'Обычный']


def test_filter_by_min_rating():
    good = _movie('Хороший', ['комедия'], ['США'], imdb=8.0, imdb_votes=100000)
    bad = _movie('Плохой', ['комедия'], ['США'], imdb=5.0, imdb_votes=100000)
    result = filter_movies_by_quality([good, bad], min_rating=6.0)
    assert [m['name'] for m in result] == ['Хороший']


def test_filter_by_min_votes():
    popular = _movie('Популярный', ['комедия'], ['США'], imdb=8.0, imdb_votes=100000)
    obscure = _movie('Неизвестный', ['комедия'], ['США'], imdb=8.0, imdb_votes=10)
    result = filter_movies_by_quality([popular, obscure], min_rating=6.0)
    assert [m['name'] for m in result] == ['Популярный']


def test_filter_excludes_anime_by_default():
    anime = _movie('Аниме', ['аниме'], ['Япония'], imdb=8.0, imdb_votes=100000)
    result = filter_movies_by_quality([anime], min_rating=6.0)
    assert result == []


def test_filter_keeps_anime_when_requested():
    anime = _movie('Аниме', ['аниме'], ['Япония'], imdb=8.0, imdb_votes=100000)
    result = filter_movies_by_quality([anime], min_rating=6.0, exclude_anime=False)
    assert [m['name'] for m in result] == ['Аниме']


def test_filter_sorts_by_weighted_rating():
    mid = _movie('Средний', ['драма'], ['США'], imdb=7.0, imdb_votes=100000)
    top = _movie('Топ', ['драма'], ['США'], imdb=9.0, imdb_votes=100000)
    result = filter_movies_by_quality([mid, top], min_rating=6.0)
    assert [m['name'] for m in result] == ['Топ', 'Средний']


def test_country_priority_all_priority_countries():
    assert get_country_priority(_movie('Фильм', [], ['США'])) == 0
    assert get_country_priority(_movie('Фильм', [], ['США', 'Канада'])) == 0


def test_country_priority_coproduction():
    assert get_country_priority(_movie('Фильм', [], ['США', 'Франция'])) == 1


def test_country_priority_no_priority_countries():
    assert get_country_priority(_movie('Фильм', [], ['Франция', 'Германия'])) == 2


def test_country_priority_no_countries():
    assert get_country_priority(_movie('Фильм', [], [])) == 2


def test_filter_prioritizes_priority_countries_over_rating():
    pure = _movie('Чистый США', ['драма'], ['США'], imdb=7.0, imdb_votes=100000)
    co = _movie('Копродукция', ['драма'], ['США', 'Франция'], imdb=9.0, imdb_votes=100000)
    other = _movie('Без приоритета', ['драма'], ['Франция'], imdb=8.5, imdb_votes=100000)
    result = filter_movies_by_quality(
        [other, co, pure], min_rating=6.0, prioritize_english_speaking=True
    )
    assert [m['name'] for m in result] == ['Чистый США', 'Копродукция', 'Без приоритета']


def test_filter_within_priority_level_sorts_by_rating():
    top = _movie('Топ', ['драма'], ['США'], imdb=9.0, imdb_votes=100000)
    mid = _movie('Средний', ['драма'], ['США'], imdb=7.0, imdb_votes=100000)
    result = filter_movies_by_quality(
        [mid, top], min_rating=6.0, prioritize_english_speaking=True
    )
    assert [m['name'] for m in result] == ['Топ', 'Средний']


# --- B3: extract_imdb_id (join-ключ IMDb ID из externalId) ---


def test_imdb_id_extracted_from_external_id():
    movie = {'externalId': {'imdb': 'tt0111161', 'tmdb': 278, 'trakt': None}}
    assert extract_imdb_id(movie) == 'tt0111161'


def test_imdb_id_strips_whitespace():
    assert extract_imdb_id({'externalId': {'imdb': '  tt0111161 '}}) == 'tt0111161'


def test_imdb_id_absent_external_id():
    """Нет externalId (покрытие ~69% базы) — штатный None."""
    assert extract_imdb_id({'id': 326, 'name': 'Фильм'}) is None


def test_imdb_id_external_id_not_dict():
    assert extract_imdb_id({'externalId': 'tt0111161'}) is None
    assert extract_imdb_id({'externalId': ['tt0111161']}) is None


def test_imdb_id_none_value():
    assert extract_imdb_id({'externalId': {'imdb': None, 'tmdb': 278}}) is None


def test_imdb_id_empty_or_whitespace_string():
    assert extract_imdb_id({'externalId': {'imdb': ''}}) is None
    assert extract_imdb_id({'externalId': {'imdb': '   '}}) is None


def test_imdb_id_non_string_value():
    """Не-строка (число и пр.) считается мусором — None."""
    assert extract_imdb_id({'externalId': {'imdb': 111161}}) is None


def test_imdb_id_empty_movie_dict():
    assert extract_imdb_id({}) is None
