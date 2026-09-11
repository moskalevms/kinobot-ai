import pytest

from utils.movie_filter import (
    apply_rt_to_score,
    extract_critics_fields,
    extract_imdb_id,
    filter_movies_by_quality,
    get_country_priority,
    get_critics_tier,
    get_weighted_rating,
    is_russian_content,
    rerank_with_rt,
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


# --- B6: RT в ранжировании — get_critics_tier (выделение ступени fc из A1) ---


def test_critics_tier_values():
    """Ступень fc: +0.3 / −0.3 / 0.0 — те же пороги, что в A1."""
    fresh = _movie('Ф', ['драма'], ['США'], imdb=7.0, fc=8.4, fc_votes=147)
    flop = _movie('Ф', ['драма'], ['США'], imdb=7.0, fc=2.5, fc_votes=30)
    neutral = _movie('Ф', ['драма'], ['США'], imdb=7.0, fc=6.0, fc_votes=100)
    few_votes = _movie('Ф', ['драма'], ['США'], imdb=7.0, fc=9.0, fc_votes=15)
    no_data = _movie('Ф', ['драма'], ['США'], imdb=7.0)
    assert get_critics_tier(fresh) == pytest.approx(0.3)
    assert get_critics_tier(flop) == pytest.approx(-0.3)
    assert get_critics_tier(neutral) == 0.0
    assert get_critics_tier(few_votes) == 0.0
    assert get_critics_tier(no_data) == 0.0


# --- B6: RT в ранжировании — apply_rt_to_score (чистая функция s1 → s2) ---


def test_rt_normalization_percent_to_ten_scale():
    """Нормализация rt/10 обязательна: rt=100 → цель шринка 10.0, не 100."""
    # 7.0 + 0.2·(10.0 − 7.0) = 7.6, ступень +0.3 (100 >= 75) → 7.9
    assert apply_rt_to_score(7.0, 100) == pytest.approx(7.9)


def test_rt_shrink_toward_higher_consensus():
    # 7.0 + 0.2·(9.0 − 7.0) = 7.4, ступень +0.3 (90 >= 75) → 7.7
    assert apply_rt_to_score(7.0, 90) == pytest.approx(7.7)


def test_rt_shrink_toward_lower_consensus():
    # 7.0 + 0.2·(3.0 − 7.0) = 6.2, ступень −0.3 (30 <= 40) → 5.9
    assert apply_rt_to_score(7.0, 30) == pytest.approx(5.9)


def test_rt_tier_boundary_exactly_75():
    """Граница +0.3 нестрогая: rt=75 → 7.0 + 0.2·0.5 = 7.1, +0.3 → 7.4."""
    assert apply_rt_to_score(7.0, 75) == pytest.approx(7.4)


def test_rt_tier_boundary_exactly_40():
    """Граница −0.3 нестрогая: rt=40 → 7.0 + 0.2·(−3.0) = 6.4, −0.3 → 6.1."""
    assert apply_rt_to_score(7.0, 40) == pytest.approx(6.1)


def test_rt_neutral_no_tier():
    """rt=60 (между 40 и 75): только шринк → 7.0 + 0.2·(−1.0) = 6.8."""
    assert apply_rt_to_score(7.0, 60) == pytest.approx(6.8)


def test_rt_none_score_unchanged():
    """rt=None (нет IMDb ID / сбой / флаг выключен) → s1 без изменений."""
    assert apply_rt_to_score(7.0, None) == 7.0


def test_rt_russian_search_score_unchanged():
    """Российская ветка не затронута даже при наличии rt."""
    assert apply_rt_to_score(7.0, 91, is_russian_search=True) == 7.0
    assert apply_rt_to_score(8.5, 10, is_russian_search=True) == 8.5


def test_rt_non_numeric_score_unchanged():
    """Нечисловой rt — «нет данных»: s1 без изменений."""
    assert apply_rt_to_score(7.0, 'много') == 7.0  # type: ignore[arg-type]


def test_rt_zero_is_valid_penalty():
    """rt=0 — валидные 0% «свежести»: шринк к 0 и ступень −0.3."""
    # 7.0 + 0.2·(0 − 7.0) = 5.6, ступень −0.3 (0 <= 40) → 5.3
    assert apply_rt_to_score(7.0, 0) == pytest.approx(5.3)


def test_rt_zero_base_score_unchanged():
    """s1 == 0.0 — маркер «нет зрительских рейтингов»: коррекция не применяется."""
    assert apply_rt_to_score(0.0, 91) == 0.0


def test_rt_combined_tier_clamp_same_sign():
    """Ступени fc(+0.3) и rt(+0.3) суммарно клампятся в +0.3, не +0.6."""
    # s1 = 7.58 (fc-шринк 7.28 + ступень fc +0.3), rt=91:
    # шринк rt: 7.58 + 0.2·(9.1 − 7.58) = 7.884; суммарная ступень +0.3
    # (кламп) — та же, что уже внутри s1 → итог 7.884
    assert apply_rt_to_score(7.58, 91, critics_tier=0.3) == pytest.approx(7.884)


def test_rt_combined_tier_clamp_opposite_sign():
    """Ступень fc +0.3 и ступень rt −0.3 гасят друг друга (сумма 0)."""
    # s1 = 7.3 (ступень fc +0.3 уже внутри), rt=30:
    # шринк rt: 7.3 + 0.2·(3.0 − 7.3) = 6.44; итого 6.44 − 0.3 + 0 = 6.14
    assert apply_rt_to_score(7.3, 30, critics_tier=0.3) == pytest.approx(6.14)


def test_rt_combined_tier_clamp_both_negative():
    """Ступени fc(−0.3) и rt(−0.3) суммарно клампятся в −0.3, не −0.6."""
    # s1 = 5.8 (ступень fc −0.3 уже внутри), rt=20:
    # шринк rt: 5.8 + 0.2·(2.0 − 5.8) = 5.04; итого 5.04 + 0.3 − 0.3 = 5.04
    assert apply_rt_to_score(5.8, 20, critics_tier=-0.3) == pytest.approx(5.04)


def test_rt_score_clamped_to_ten():
    # 9.9 + 0.2·(10.0 − 9.9) = 9.92, ступень +0.3 → 10.22 → кламп 10.0
    assert apply_rt_to_score(9.9, 100) == 10.0


def test_rt_score_clamped_to_zero():
    # 0.1 + 0.2·(0 − 0.1) = 0.08, ступень −0.3 → −0.22 → кламп 0.0
    assert apply_rt_to_score(0.1, 0) == 0.0


def test_rt_formula_end_to_end_with_fc():
    """Связка A1 → B6: s1 = get_weighted_rating, ступень = get_critics_tier."""
    movie = _movie('Ф', ['драма'], ['США'], imdb=7.0, imdb_votes=1000, fc=8.4, fc_votes=147)
    s1 = get_weighted_rating(movie)
    tier_fc = get_critics_tier(movie)
    # s1: 7.0 + 0.2·(8.4 − 7.0) = 7.28, ступень +0.3 → 7.58
    assert s1 == pytest.approx(7.58)
    assert tier_fc == pytest.approx(0.3)
    # s2: шринк rt (7.884), суммарный кламп ступеней +0.3 → без надбавки
    assert apply_rt_to_score(s1, 91, critics_tier=tier_fc) == pytest.approx(7.884)


# --- B6: RT в ранжировании — rerank_with_rt (пересортировка финала) ---


def _ranked(name, score, rt=None, priority=0, tier=0.0):
    """Финальный словарь фильма после форматирования и обогащения (B5+B6)."""
    return {
        'name': name,
        'weighted_score': score,
        'rt_score': rt,
        'metascore': None,
        'country_priority': priority,
        'critics_tier': tier,
    }


def test_rerank_boosts_movie_with_high_rt():
    """rt=91 поднимает фильм 7.4 (→8.04) выше фильма 7.6 без rt."""
    a = _ranked('A', 7.6)
    b = _ranked('B', 7.4, rt=91)
    result = rerank_with_rt([a, b])
    assert [m['name'] for m in result] == ['B', 'A']
    assert result[0]['weighted_score'] == pytest.approx(8.04)


def test_rerank_demotes_movie_with_low_rt():
    """rt=20 опускает фильм 7.5 (→6.1) ниже фильма 7.0 без rt."""
    a = _ranked('A', 7.5, rt=20)
    b = _ranked('B', 7.0)
    result = rerank_with_rt([a, b])
    assert [m['name'] for m in result] == ['B', 'A']


def test_rerank_without_rt_keeps_order():
    """Обратная совместимость: ни одного rt_score → порядок фазы 0+A1+B5."""
    a = _ranked('A', 7.6)
    b = _ranked('B', 7.4)
    result = rerank_with_rt([a, b])
    assert [m['name'] for m in result] == ['A', 'B']
    assert result[0]['weighted_score'] == 7.6
    assert result[1]['weighted_score'] == 7.4


def test_rerank_stable_on_equal_scores():
    """Равные итоги сохраняют относительный порядок (стабильность)."""
    a = _ranked('A', 7.0)
    b = _ranked('B', 7.0)
    assert [m['name'] for m in rerank_with_rt([a, b])] == ['A', 'B']
    assert [m['name'] for m in rerank_with_rt([b, a])] == ['B', 'A']


def test_rerank_respects_country_priority_groups():
    """Приоритетная группировка по странам не ломается RT-бустом."""
    usa = _ranked('США-фильм', 7.0, priority=0)
    france = _ranked('Франция-фильм', 6.8, rt=91, priority=2)
    result = rerank_with_rt([france, usa])
    assert [m['name'] for m in result] == ['США-фильм', 'Франция-фильм']


def test_rerank_russian_search_is_noop():
    """Российская ветка: ни пересчёта, ни пересортировки."""
    a = _ranked('A', 8.0)
    b = _ranked('B', 7.0, rt=91)
    result = rerank_with_rt([a, b], is_russian_search=True)
    assert [m['name'] for m in result] == ['A', 'B']
    assert b['weighted_score'] == 7.0


def test_rerank_without_weighted_score_is_noop():
    """Словари без числового weighted_score не пересортировываются (защита)."""
    a = {'name': 'A', 'rt_score': 91}
    b = {'name': 'B', 'weighted_score': 7.0, 'rt_score': 10, 'critics_tier': 0.0}
    result = rerank_with_rt([a, b])
    assert [m['name'] for m in result] == ['A', 'B']
    assert b['weighted_score'] == 7.0


def test_rerank_composition_unchanged():
    """Пересортировка не меняет состав списка (B5: выдача цела)."""
    movies = [_ranked('A', 7.0, rt=10), _ranked('B', 7.5), _ranked('C', 6.9, rt=95)]
    result = rerank_with_rt(movies)
    assert len(result) == 3
    assert sorted(m['name'] for m in result) == ['A', 'B', 'C']
    # C: 6.9 + 0.2·(9.5 − 6.9) = 7.42, ступень +0.3 → 7.72; B: 7.5 без rt;
    # A: 7.0 + 0.2·(1.0 − 7.0) = 5.8, ступень −0.3 → 5.5
    assert [m['name'] for m in result] == ['C', 'B', 'A']


def test_rerank_empty_list_is_noop():
    assert rerank_with_rt([]) == []
