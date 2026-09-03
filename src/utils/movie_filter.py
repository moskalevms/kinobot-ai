# src/utils/movie_filter.py
from typing import List, Dict, Optional, Set
import logging

logger = logging.getLogger(__name__)

# Жанры, исключаемые из выдачи по умолчанию. Единый перечень для всех
# контуров фильтрации; если пользователь явно запрашивает такой жанр,
# он передаётся в allowed_excluded_genres.
EXCLUDED_GENRES = {
    'мюзикл', 'концерт', 'документальный', 'документалка',
    'короткометражка', 'короткометражный', 'биография',
    'артхаус', 'реалити-тв', 'ток-шоу', 'церемония',
    'эротика', 'для взрослых', 'adult', '18+', 'спорт', 'спортивный',
    'новости', 'новостной',
}

# Жанры «музыкального» контента. Кинопоиск часто помечает концерты,
# лайв-выступления и музыкальные документалки только жанром «музыка»
# (например, «Metallica: Live Shit», «Режиссёр Мишель Гондри в работе»),
# поэтому по жанрам «концерт»/«документальный» они не отсеиваются.
# Тайтл, у которого ВСЕ жанры входят в этот набор, считается
# концертным/музыкальным контентом и исключается по умолчанию.
MUSIC_ONLY_GENRES = {'музыка', 'концерт', 'мюзикл'}


# Страны с приоритетной выдачей (англоязычные)
HIGH_PRIORITY_COUNTRIES = {
    'сша', 'usa', 'united states', 'канада', 'canada',
    'великобритания', 'uk', 'united kingdom',
}


def get_country_priority(movie: Dict) -> int:
    """Приоритет фильма по странам производства.

    0 — все страны входят в приоритетный перечень;
    1 — копродукция: приоритетные страны есть, но не все;
    2 — приоритетных стран нет (включая фильм без стран).
    """
    countries = movie.get('countries', [])
    if not countries:
        return 2

    country_names = {c.get('name', '').lower() for c in countries if isinstance(c, dict)}
    matches = country_names & HIGH_PRIORITY_COUNTRIES
    if not matches:
        return 2
    if matches == country_names:
        return 0
    return 1


def is_russian_content(movie: Dict) -> bool:
    countries = movie.get('countries', [])
    if not countries:
        return False

    country_names = {c.get('name', '').lower() for c in countries if isinstance(c, dict)}
    russian_keywords = {'россия', 'russia', 'российская федерация', 'russian federation', 'ссср', 'soviet union'}
    return any(rk in country_names for rk in russian_keywords)


def get_weighted_rating(movie: Dict, is_russian_search: bool = False) -> float:
    rating = movie.get('rating', {})
    imdb_rating = rating.get('imdb')
    kp_rating = rating.get('kp')

    if is_russian_search or is_russian_content(movie):
        if kp_rating is not None:
            return kp_rating
        elif imdb_rating is not None:
            return imdb_rating * 0.9
        else:
            return 0.0
    else:
        if imdb_rating is not None:
            return imdb_rating
        elif kp_rating is not None:
            return kp_rating * 0.8
        else:
            return 0.0


def is_music_only_content(movie: Dict) -> bool:
    """Тайтл, у которого все жанры музыкальные (концерт/лайв/муз. документалка)."""
    genres = movie.get('genres', [])
    genre_names = {g.get('name', '').lower() for g in genres if isinstance(g, dict)}
    genre_names.discard('')
    return bool(genre_names) and genre_names <= MUSIC_ONLY_GENRES


def should_exclude_by_genre(movie: Dict, allowed_excluded_genres: Optional[Set[str]] = None) -> bool:
    if allowed_excluded_genres is None:
        allowed_excluded_genres = set()

    genres = movie.get('genres', [])
    genre_names = {g.get('name', '').lower() for g in genres if isinstance(g, dict)}
    genre_names.discard('')

    allowed_match = genre_names & allowed_excluded_genres

    found_excluded = genre_names & EXCLUDED_GENRES
    if found_excluded and not allowed_match:
        return True

    if is_music_only_content(movie) and not allowed_match:
        return True

    return False


def filter_movies_by_quality(
        movies: List[Dict],
        year: Optional[int] = None,
        min_rating: float = 6.0,
        min_votes_override: Optional[int] = None,
        exclude_anime: bool = True,
        prioritize_english_speaking: bool = False,
        allowed_excluded_genres: Optional[Set[str]] = None,
        is_russian_search: bool = False
) -> List[Dict]:
    if allowed_excluded_genres is None:
        allowed_excluded_genres = set()

    def _calculate_min_votes(y: Optional[int]) -> int:
        if not y:
            return 1000
        if y >= 2020:
            return 100
        elif y >= 2010:
            return 500
        elif y >= 2000:
            return 1000
        else:
            return 5000

    min_votes = min_votes_override if min_votes_override is not None else _calculate_min_votes(year)
    filtered = []

    for movie in movies:
        if should_exclude_by_genre(movie, allowed_excluded_genres):
            name = movie.get('name', 'Unknown')
            logger.debug(f"[MovieFilter] Пропущен по жанру: {name}")
            continue

        if exclude_anime:
            genres = movie.get('genres', [])
            genre_names = {g.get('name', '').lower() for g in genres if isinstance(g, dict)}
            if 'аниме' in genre_names and 'аниме' not in allowed_excluded_genres:
                name = movie.get('name', 'Unknown')
                logger.debug(f"[MovieFilter] Пропущен аниме: {name}")
                continue

        votes = movie.get('votes', {})
        imdb_votes = votes.get('imdb') or 0
        kp_votes = votes.get('kp') or 0

        weighted_rating = get_weighted_rating(movie, is_russian_search)
        best_votes = max(imdb_votes, kp_votes)

        if weighted_rating < min_rating:
            continue
        if best_votes < min_votes:
            continue

        movie['weighted_rating'] = weighted_rating
        filtered.append(movie)

    if prioritize_english_speaking:
        def _sort_key(m):
            return (get_country_priority(m), -m.get('weighted_rating', 0))

        filtered.sort(key=_sort_key)
    else:
        def _sort_key(m):
            return -m.get('weighted_rating', 0)

        filtered.sort(key=_sort_key)

    for movie in filtered:
        if 'weighted_rating' in movie:
            del movie['weighted_rating']

    logger.info(f"[MovieFilter] Отфильтровано: {len(filtered)} фильмов из {len(movies)}")
    return filtered
