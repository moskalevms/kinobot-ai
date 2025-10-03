# src/utils/movie_filter.py
from typing import List, Dict, Optional, Set
import logging

logger = logging.getLogger(__name__)


def get_country_priority(movie: Dict) -> int:
    countries = movie.get('countries', [])
    if not countries:
        return 2

    country_names = {c.get('name', '').lower() for c in countries if isinstance(c, dict)}
    high_priority = {'сша', 'usa', 'united states', 'канада', 'canada', 'великобритания', 'uk', 'united kingdom'}
    if any(c in high_priority for c in country_names):
        return 0
    elif len(country_names) > 1 and any(c in high_priority for c in country_names):
        return 1
    else:
        return 2


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


def should_exclude_by_genre(movie: Dict, allowed_excluded_genres: Set[str] = None) -> bool:
    if allowed_excluded_genres is None:
        allowed_excluded_genres = set()

    excluded_genres = {
        'мюзикл', 'концерт', 'документальный', 'документалка',
        'короткометражка', 'короткометражный', 'биография',
        'артхаус', 'реалити-тв', 'ток-шоу', 'церемония',
        'эротика', 'для взрослых', 'adult', '18+', 'спорт', 'спортивный',
        'новости', 'новостной'  # ← добавлены новости
    }

    genres = movie.get('genres', [])
    genre_names = {g.get('name', '').lower() for g in genres if isinstance(g, dict)}

    found_excluded = genre_names & excluded_genres
    if found_excluded and not (genre_names & allowed_excluded_genres):
        return True

    return False


def filter_movies_by_quality(
        movies: List[Dict],
        year: Optional[int] = None,
        min_rating: float = 6.0,
        min_votes_override: Optional[int] = None,
        exclude_anime: bool = True,
        prioritize_english_speaking: bool = False,
        allowed_excluded_genres: Set[str] = None,
        is_russian_search: bool = False
) -> List[Dict]:
    if allowed_excluded_genres is None:
        allowed_excluded_genres = set()

    current_year = 2025

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

        rating = movie.get('rating', {})
        votes = movie.get('votes', {})
        imdb_rating = rating.get('imdb')
        kp_rating = rating.get('kp')
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