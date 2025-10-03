# src/movie_agent.py
import logging
from typing import List, Dict, Optional, Tuple
from enum import Enum
from .kinopoisk_client import KinopoiskClient
from .recommendation_engine import RecommendationEngine
from .config import KINOPOISK_API_KEY

logger = logging.getLogger(__name__)

CANDIDATE_LIMIT = 150

class SearchStrategy(Enum):
    TOP_GENRE = 1
    BY_PERSON = 2
    FREE_TEXT = 3

class MovieAgent:
    def __init__(self, use_api: bool = True):
        self.use_api = use_api
        self.kinopoisk_client = KinopoiskClient(api_key=KINOPOISK_API_KEY)
        self.recommendation_engine = RecommendationEngine(self.kinopoisk_client)
        self._search_cache = {}
        self._last_search_failed = False  # Флаг для отслеживания неудачных поисков

    def _get_cache_key(self, genre_name, year, year_range, actor, director, country, min_imdb_rating, limit, movie_type, query) -> str:
        parts = [
            genre_name or '',
            str(year),
            str(year_range),
            actor or '',
            director or '',
            country or '',
            str(min_imdb_rating),
            str(limit),
            movie_type,
            query or ''
        ]
        return '_'.join(parts)

    def recommend_movies(
        self,
        genre_name: Optional[str] = None,
        year: Optional[int] = None,
        year_range: Optional[tuple] = None,
        actor: Optional[str] = None,
        director: Optional[str] = None,
        country: Optional[str] = None,
        min_imdb_rating: float = 6.5,
        limit: int = 8,
        movie_type: str = 'movie',
        query: Optional[str] = None
    ) -> List[Dict]:
        from .config import CACHE_TTL
        import time

        current_year = 2025
        if year_range and year_range[1] > current_year:
            year_range = (year_range[0], current_year)
            logger.info(f"Корректировка year_range на текущий год: {year_range}")

        cache_key = self._get_cache_key(genre_name, year, year_range, actor, director, country, min_imdb_rating, limit, movie_type, query)

        # Очищаем кэш если предыдущий поиск был неудачным
        if self._last_search_failed:
            self._search_cache.clear()
            self._last_search_failed = False
            logger.info("[MovieAgent] Кэш очищен из-за предыдущей ошибки поиска")

        # Проверка кэша с TTL
        if cache_key in self._search_cache:
            cached_data, timestamp = self._search_cache[cache_key]
            if time.time() - timestamp < CACHE_TTL:
                logger.info("[MovieAgent] Кэш HIT")
                return cached_data
            else:
                del self._search_cache[cache_key]

        is_top = any(word in (query or '').lower() for word in ['топ', 'лучш', 'рейтинг', 'best', 'top']) if query else False

        try:
            movies = self.recommendation_engine.get_recommendations(
                genre_name=genre_name,
                year=year,
                year_range=year_range,
                actor=actor,
                director=director,
                country=country,
                min_imdb_rating=min_imdb_rating,
                limit=limit,
                movie_type=movie_type,
                query=query,
                is_top=is_top
            )

            # Устанавливаем флаг неудачного поиска если результатов нет
            if not movies:
                self._last_search_failed = True
                logger.info("[MovieAgent] Поиск не дал результатов, устанавливаем флаг ошибки")
            else:
                self._search_cache[cache_key] = (movies, time.time())

            logger.info(f"Найдено {movie_type}: {len(movies)}")
            return movies

        except Exception as e:
            logger.error(f"Ошибка в recommend_movies: {e}", exc_info=True)
            self._last_search_failed = True
            return []

    def search_by_title(self, title: str) -> List[Dict]:
        try:
            data = self.kinopoisk_client.search_movie_by_title(title, limit=10)
            if not data or not data.get('docs'):
                return []

            docs = data['docs']
            user_title_lower = title.lower().strip()

            movie_candidates = [m for m in docs if m.get('type') == 'movie']

            if not movie_candidates:
                return []

            best_match = None
            for movie in movie_candidates:
                name = movie.get('name') or ''
                alt_names = movie.get('alternativeName') or []

                if not isinstance(alt_names, list):
                    alt_names = [alt_names] if alt_names else []

                all_names = [name] + [n for n in alt_names if n]
                all_names_lower = [str(n).lower().strip() for n in all_names if n]

                if any(user_title_lower == n for n in all_names_lower):
                    best_match = movie
                    break

            if best_match is None:
                best_match = movie_candidates[0]

            genres = ', '.join([g['name'] for g in best_match.get('genres', []) if g.get('name')])
            countries = ', '.join([c['name'] for c in best_match.get('countries', []) if c.get('name')])
            rating_imdb = best_match.get('rating', {}).get('imdb')
            rating_kp = best_match.get('rating', {}).get('kp')
            poster_url = best_match.get('poster', {}).get('url', '').strip()

            return [{
                'id': best_match.get('id'),
                'title': best_match.get('name') or '—',
                'year': best_match.get('year'),
                'genre': genres,
                'country': countries,
                'rating': rating_imdb or rating_kp or '—',
                'rating_imdb': rating_imdb,
                'rating_kp': rating_kp,
                'description': (best_match.get('description') or '')[:500],
                'poster_url': poster_url
            }]

        except Exception as e:
            logger.warning(f"Ошибка поиска по названию '{title}': {e}", exc_info=True)
            return []

    def health_check(self) -> bool:
        try:
            test_movies = self.recommend_movies(genre_name='комедия', limit=1)
            return test_movies is not None and (not isinstance(test_movies, dict) or 'error' not in test_movies)
        except Exception:
            return False

    def clear_cache(self):
        self._search_cache.clear()
        self._last_search_failed = False
        logger.info("[MovieAgent] Кэш очищен")