# src/recommendation_engine.py
import logging
from typing import List, Dict, Optional, Tuple, Set
from .utils.movie_filter import filter_movies_by_quality
from .kinopoisk_client import KinopoiskClient
logger = logging.getLogger(__name__)


class RecommendationEngine:
    def __init__(self, kinopoisk_client: KinopoiskClient):
        self.kinopoisk_client = kinopoisk_client

    async def get_recommendations(
        self,
        session,
        genre_name: Optional[str] = None,
        year: Optional[int] = None,
        year_range: Optional[Tuple[int, int]] = None,
        actor: Optional[str] = None,
        director: Optional[str] = None,
        country: Optional[str] = None,
        min_imdb_rating: float = 6.5,
        limit: int = 8,
        movie_type: str = 'movie',
        query: Optional[str] = None,
        is_top: bool = False
    ) -> List[Dict]:
        allowed_excluded_genres = set()
        excluded_genres_list = [
            'мюзикл', 'концерт', 'документальный', 'документалка',
            'короткометражка', 'короткометражный', 'биография',
            'артхаус', 'реалити-тв', 'ток-шоу', 'церемония',
            'эротика', 'для взрослых', 'adult', '18+', 'спорт', 'спортивный',
            'новости', 'новостной'
        ]
        is_russian_search = country and country.lower() in ['россия', 'russia', 'российская федерация']
        if genre_name and genre_name.lower() in excluded_genres_list:
            allowed_excluded_genres.add(genre_name.lower())
        if query:
            query_lower = query.lower()
            for excluded_genre in excluded_genres_list:
                if excluded_genre in query_lower:
                    allowed_excluded_genres.add(excluded_genre)
            if genre_name and genre_name.lower() in ['анимация', 'мультфильм']:
                if 'аниме' in (query or '').lower():
                    genre_name = 'аниме'
                elif genre_name.lower() == 'анимация':
                    genre_name = 'мультфильм'

        logger.info(f"Разрешённые исключаемые жанры: {allowed_excluded_genres}")
        logger.info(f"Российский поиск: {is_russian_search}")

        if year or year_range:
            return await self._get_range_recommendations(
                session,
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
                is_top=is_top,
                allowed_excluded_genres=allowed_excluded_genres,
                is_russian_search=is_russian_search
            )
        else:
            return await self._get_general_recommendations(
                session,
                genre_name=genre_name,
                actor=actor,
                director=director,
                country=country,
                min_imdb_rating=min_imdb_rating,
                limit=limit,
                movie_type=movie_type,
                query=query,
                is_top=is_top,
                allowed_excluded_genres=allowed_excluded_genres,
                is_russian_search=is_russian_search
            )

    async def _get_range_recommendations(
        self,
        session,
        genre_name: Optional[str] = None,
        year: Optional[int] = None,
        year_range: Optional[Tuple[int, int]] = None,
        actor: Optional[str] = None,
        director: Optional[str] = None,
        country: Optional[str] = None,
        min_imdb_rating: float = 6.0,
        limit: int = 8,
        movie_type: str = 'movie',
        query: Optional[str] = None,
        is_top: bool = False,
        allowed_excluded_genres: Set[str] = None,
        is_russian_search: bool = False
    ) -> List[Dict]:
        if allowed_excluded_genres is None:
            allowed_excluded_genres = set()
        candidates = []
        min_votes_override = None
        actual_genre = genre_name

        if genre_name == 'аниме':
            search_types = ['anime', 'tv-series', 'movie']
        else:
            search_types = [movie_type]

        for search_type in search_types:
            if len(candidates) >= limit * 2:
                break

            # 🔧 ИСПРАВЛЕНИЕ: top250 НЕ ИСПОЛЬЗУЕТСЯ при year_range
            if is_top and year_range is None:
                top_data = await self.kinopoisk_client.search_recommendation(
                    session,
                    genre=actual_genre,
                    year=year,
                    country=country,
                    limit=250,
                    movie_type=search_type
                )
                if top_data and top_data.get('docs'):
                    candidates.extend(top_data['docs'])

            search_data = await self.kinopoisk_client.search_movies(
                session,
                genre=actual_genre,
                year=year,
                year_range=year_range,
                actor=actor,
                director=director,
                imdb_rating_min=min_imdb_rating,
                kp_rating_min=min_imdb_rating - 0.5,
                movie_type=search_type,
                query=query,
                limit=250,
                country=country
            )
            if search_data and search_data.get('docs'):
                candidates.extend(search_data['docs'])

        seen_ids = set()
        unique_candidates = []
        for movie in candidates:
            mid = movie.get('id')
            if mid and mid not in seen_ids:
                seen_ids.add(mid)
                unique_candidates.append(movie)

        if year_range:
            min_votes_override = 100 if year_range[0] >= 2020 else 500

        filtered = filter_movies_by_quality(
            unique_candidates,
            year=year,
            min_rating=min_imdb_rating,
            min_votes_override=min_votes_override,
            exclude_anime=(actual_genre != 'аниме'),
            prioritize_english_speaking=not is_russian_search,
            allowed_excluded_genres=allowed_excluded_genres,
            is_russian_search=is_russian_search
        )

        return self._format_movies_list(filtered, limit)

    async def _get_general_recommendations(
        self,
        session,
        genre_name: Optional[str] = None,
        actor: Optional[str] = None,
        director: Optional[str] = None,
        country: Optional[str] = None,
        min_imdb_rating: float = 6.5,
        limit: int = 8,
        movie_type: str = 'movie',
        query: Optional[str] = None,
        is_top: bool = False,
        allowed_excluded_genres: Set[str] = None,
        is_russian_search: bool = False
    ) -> List[Dict]:
        if allowed_excluded_genres is None:
            allowed_excluded_genres = set()
        candidates = []
        actual_genre = genre_name

        if genre_name == 'аниме':
            search_types = ['anime', 'tv-series', 'movie']
        else:
            search_types = [movie_type]

        for search_type in search_types:
            if len(candidates) >= limit * 2:
                break

            # Здесь year_range всегда None, поэтому top250 можно использовать
            if is_top:
                top_data = await self.kinopoisk_client.search_recommendation(
                    session,
                    genre=actual_genre,
                    country=country,
                    limit=250,
                    movie_type=search_type
                )
                if top_data and top_data.get('docs'):
                    candidates.extend(top_data['docs'])

            search_data = await self.kinopoisk_client.search_movies(
                session,
                genre=actual_genre,
                actor=actor,
                director=director,
                country=country,
                imdb_rating_min=min_imdb_rating,
                kp_rating_min=min_imdb_rating - 0.5,
                movie_type=search_type,
                query=query,
                limit=250
            )
            if search_data and search_data.get('docs'):
                candidates.extend(search_data['docs'])

        seen_ids = set()
        unique_candidates = [m for m in candidates if m.get('id') not in seen_ids and not seen_ids.add(m.get('id'))]

        filtered = filter_movies_by_quality(
            unique_candidates,
            min_rating=min_imdb_rating,
            exclude_anime=(actual_genre != 'аниме'),
            prioritize_english_speaking=not is_russian_search,
            allowed_excluded_genres=allowed_excluded_genres,
            is_russian_search=is_russian_search
        )

        return self._format_movies_list(filtered, limit)

    def _format_movies_list(self, movies: List[Dict], limit: int) -> List[Dict]:
        formatted = []
        for movie in movies:
            if len(formatted) >= limit:
                break
            if not isinstance(movie, dict) or movie.get('id') is None:
                logger.warning(f"[RecommendationEngine] Пропущен некорректный фильм: {movie}")
                continue
            title = movie.get('name')
            if not title or not str(title).strip():
                logger.warning(f"[RecommendationEngine] Пропущен фильм без основного названия: ID={movie.get('id')}")
                continue

            genres = []
            for g in movie.get('genres', []):
                if isinstance(g, dict) and g.get('name'):
                    genres.append(g['name'])
            genre_str = ', '.join(genres)

            countries = []
            for c in movie.get('countries', []):
                if isinstance(c, dict) and c.get('name'):
                    countries.append(c['name'])
            country_str = ', '.join(countries)

            rating_obj = movie.get('rating', {})
            rating_imdb = rating_obj.get('imdb')
            rating_kp = rating_obj.get('kp')

            from .utils.movie_filter import is_russian_content
            is_russian = is_russian_content(movie)

            if is_russian and rating_kp is not None:
                best_rating = rating_kp
                rating_source = "КП"
            elif rating_imdb is not None:
                best_rating = rating_imdb
                rating_source = "IMDB"
            elif rating_kp is not None:
                best_rating = rating_kp
                rating_source = "КП"
            else:
                best_rating = '—'
                rating_source = "—"

            description = (movie.get('description') or '')[:500]
            poster_url = ''
            poster = movie.get('poster')
            if isinstance(poster, dict):
                poster_url = poster.get('url', '')

            formatted_movie = {
                'id': movie.get('id'),
                'title': title.strip(),
                'year': movie.get('year'),
                'genre': genre_str,
                'country': country_str,
                'rating': best_rating,
                'rating_imdb': rating_imdb,
                'rating_kp': rating_kp,
                'rating_source': rating_source,
                'description': description,
                'poster_url': poster_url,
                'kinopoisk_url': f"https://www.kinopoisk.ru/film/{movie.get('id')}/" if movie.get('id') else None,
                'type': movie.get('type', 'movie')
            }
            formatted.append(formatted_movie)

        logger.info(f"[RecommendationEngine] Отформатировано фильмов: {len(formatted)} из запрошенных {limit}")
        return formatted