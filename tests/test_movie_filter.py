from utils.movie_filter import (
    filter_movies_by_quality,
    get_weighted_rating,
    is_russian_content,
    should_exclude_by_genre,
)


def _movie(name, genres, countries, imdb=None, kp=None, imdb_votes=0, kp_votes=0):
    return {
        'name': name,
        'genres': [{'name': g} for g in genres],
        'countries': [{'name': c} for c in countries],
        'rating': {'imdb': imdb, 'kp': kp},
        'votes': {'imdb': imdb_votes, 'kp': kp_votes},
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
