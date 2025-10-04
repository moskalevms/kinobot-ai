import os
import logging
from pathlib import Path
from typing import Optional, List, Dict, Union, Any
from dataclasses import dataclass
from enum import Enum

import pandas as pd
from dotenv import load_dotenv

from src.client.kinopoisk_client import KinopoiskClient
from src.utils.movie_filter import filter_movies_by_quality

logger = logging.getLogger(__name__)
load_dotenv()


@dataclass
class SearchRequest:
    genre: Optional[str] = None
    year: Optional[int] = None
    actor: Optional[str] = None
    director: Optional[str] = None
    studio: Optional[str] = None
    country: Optional[str] = None
    min_rating: Optional[float] = None
    limit: int = 10  # ← сколько вернуть пользователю
    movie_type: str = 'movie'
    query: Optional[str] = None
    is_top_request: bool = False
    is_new_request: bool = False


# Фиксированный лимит для сбора кандидатов (не зависит от запроса пользователя)
CANDIDATE_LIMIT = 150


class SearchStrategy(Enum):
    TOP_GENRE = "top_genre"
    BY_PERSON = "by_person"
    FREE_TEXT = "free_text"
    BY_TITLE = "by_title"


class MovieAgent:
    def __init__(self, use_api=True):
        self.use_api = use_api
        self.data_path = Path(__file__).parent.parent / "data" / "processed" / "imdb" / "imdb_top_1000.csv"
        self.kinopoisk_client = KinopoiskClient() if use_api else None

    def recommend_movies(
            self,
            genre_name: Optional[str] = None,
            year: Optional[int] = None,
            actor: Optional[str] = None,
            director: Optional[str] = None,
            studio: Optional[str] = None,
            country: Optional[str] = None,
            min_imdb_rating: Optional[float] = None,
            limit: int = 10,
            movie_type: str = 'movie',
            query: Optional[str] = None
    ) -> Union[List[Dict], Dict]:

        if not self.use_api or not self.kinopoisk_client:
            return self._fallback_to_csv(genre_name, year, limit)

        try:
            is_top = self._is_top_request(query)
            is_new = self._is_new_request(query, year)

            search_request = SearchRequest(
                genre=genre_name,
                year=year,
                actor=actor,
                director=director,
                studio=studio,
                country=country,
                min_rating=min_imdb_rating,
                limit=limit,
                movie_type=movie_type,
                query=query,
                is_top_request=is_top,
                is_new_request=is_new
            )

            movies_data = self._multi_strategy_search(search_request)

            if not movies_data:
                return []

            result = self._format_movies(movies_data, search_request)
            logger.info(f"Найдено фильмов: {len(result)}")
            return result[:limit]

        except Exception as e:
            logger.error(f"Ошибка в recommend_movies: {e}", exc_info=True)
            return {"error": str(e)}

    def _is_top_request(self, query: Optional[str]) -> bool:
        if not query:
            return False
        q = query.lower()
        indicators = ['лучш', 'топ', 'рейтинг', 'best', 'top']
        return any(ind in q for ind in indicators)

    def _is_new_request(self, query: Optional[str], year: Optional[int]) -> bool:
        if year and year >= 2023:
            return True
        if not query:
            return False
        new_keywords = ['новые', 'свежие', 'недавние', '2024', '2025']
        return any(keyword in query.lower() for keyword in new_keywords)

    def _determine_strategy(self, request: SearchRequest) -> SearchStrategy:
        if request.query:
            q = request.query.lower()
            if any(phrase in q for phrase in ["расскажи о", "что за фильм", "информация о", "описание фильма"]):
                return SearchStrategy.BY_TITLE
            if request.actor or request.director:
                return SearchStrategy.BY_PERSON
            if any(ind in q for ind in ['лучш', 'топ', 'top', 'best']) and any(char.isdigit() for char in q):
                return SearchStrategy.TOP_GENRE
            if request.is_top_request:
                return SearchStrategy.TOP_GENRE
        return SearchStrategy.FREE_TEXT

    def _multi_strategy_search(self, request: SearchRequest) -> List[Dict]:
        strategy = self._determine_strategy(request)
        logger.info(f"[MovieAgent] Выбрана стратегия: {strategy.value}")

        if strategy == SearchStrategy.TOP_GENRE:
            return self._search_top_genre(request)
        elif strategy == SearchStrategy.BY_PERSON:
            return self._search_by_person(request)
        elif strategy == SearchStrategy.FREE_TEXT:
            return self._search_free_text(request)
        else:
            return []

    def _search_top_genre(self, request: SearchRequest) -> List[Dict]:
        # Шаг 1: поиск в топ-250
        movies_data = self.kinopoisk_client.search_top250(
            genre=request.genre,
            year=request.year,
            country=request.country,
            limit=CANDIDATE_LIMIT
        )
        if movies_data and movies_data.get('docs'):
            filtered = filter_movies_by_quality(
                movies_data['docs'],
                year=request.year,
                min_rating=7.5
            )
            logger.info(f"[MovieAgent] После фильтрации: {len(filtered)} из {len(movies_data['docs'])}")
            if len(filtered) >= max(3, request.limit // 2):
                return filtered[:request.limit]

        # Шаг 2: fallback — обычный поиск с высоким рейтингом
        logger.info("[MovieAgent] Fallback: TOP_GENRE → обычный поиск с rating ≥7.5")
        movies_data = self.kinopoisk_client.search_movies(
            genre=request.genre,
            year=request.year,
            country=request.country,
            imdb_rating_min=7.5,
            movie_type=request.movie_type,
            limit=CANDIDATE_LIMIT
        )
        if movies_data and movies_data.get('docs'):
            filtered = filter_movies_by_quality(
                movies_data['docs'],
                year=request.year,
                min_rating=7.5
            )
            return filtered[:request.limit]
        return []

    def _search_by_person(self, request: SearchRequest) -> List[Dict]:
        movies_data = self.kinopoisk_client.search_movies(
            genre=request.genre,
            year=request.year,
            actor=request.actor,
            director=request.director,
            imdb_rating_min=request.min_rating or 6.0,
            movie_type=request.movie_type,
            country=request.country,
            limit=CANDIDATE_LIMIT
        )
        if movies_data and movies_data.get('docs'):
            filtered = filter_movies_by_quality(
                movies_data['docs'],
                year=request.year,
                min_rating=request.min_rating or 6.0,
                min_votes_override=self._calculate_min_votes_for_person(request.year)
            )
            logger.info(f"[MovieAgent] После фильтрации: {len(filtered)} из {len(movies_data['docs'])}")
            if len(filtered) >= max(3, request.limit // 2):
                return filtered[:request.limit]

        # Fallback: без фильтра по голосам
        logger.info("[MovieAgent] Fallback: поиск по персоне без фильтра голосов")
        movies_data = self.kinopoisk_client.search_movies(
            genre=request.genre,
            year=request.year,
            actor=request.actor,
            director=request.director,
            movie_type=request.movie_type,
            country=request.country,
            limit=CANDIDATE_LIMIT
        )
        if movies_data and movies_data.get('docs'):
            return [
                m for m in movies_data['docs']
                if (m.get('rating', {}).get('imdb') or m.get('rating', {}).get('kp') or 0) >= 6.0
            ][:request.limit]
        return []

    def _search_free_text(self, request: SearchRequest) -> List[Dict]:
        movies_data = self.kinopoisk_client.search_movies(
            query=request.query,
            genre=request.genre,
            year=request.year,
            country=request.country,
            imdb_rating_min=request.min_rating or 6.5,
            movie_type=request.movie_type,
            limit=CANDIDATE_LIMIT
        )
        if movies_data and movies_data.get('docs'):
            filtered = filter_movies_by_quality(
                movies_data['docs'],
                year=request.year,
                min_rating=request.min_rating or 6.5
            )
            logger.info(f"[MovieAgent] После фильтрации: {len(filtered)} из {len(movies_data['docs'])}")
            if filtered:
                return filtered[:request.limit]

        # Fallback: без query
        logger.info("[MovieAgent] Fallback: free text → search без query")
        movies_data = self.kinopoisk_client.search_movies(
            genre=request.genre,
            year=request.year,
            country=request.country,
            imdb_rating_min=request.min_rating or 6.5,
            movie_type=request.movie_type,
            limit=CANDIDATE_LIMIT
        )
        if movies_data and movies_data.get('docs'):
            filtered = filter_movies_by_quality(
                movies_data['docs'],
                year=request.year,
                min_rating=request.min_rating or 6.5
            )
            return filtered[:request.limit]
        return []

    def _calculate_min_votes_for_person(self, year: Optional[int]) -> int:
        current_year = 2025
        if not year:
            return 5000
        diff = current_year - year
        if diff <= 2:
            return 1000
        elif diff <= 5:
            return 3000
        else:
            return 5000

    def _format_movies(self, movies_data: List[Dict], request: SearchRequest) -> List[Dict]:
        formatted = []
        for movie in movies_data:
            genres = ', '.join([g['name'] for g in movie.get('genres', []) if g.get('name')])
            countries = ', '.join([c['name'] for c in movie.get('countries', []) if c.get('name')])
            rating_imdb = movie.get('rating', {}).get('imdb')
            rating_kp = movie.get('rating', {}).get('kp')
            formatted_movie = {
                'id': movie.get('id'),
                'title': movie.get('name') or '—',
                'year': movie.get('year'),
                'genre': genres,
                'country': countries,
                'rating': rating_imdb or rating_kp or '—',
                'rating_imdb': rating_imdb,
                'rating_kp': rating_kp,
                'description': (movie.get('description') or '')[:500],
                'is_us_production': self._is_us_production(movie.get('countries', []))
            }
            formatted.append(formatted_movie)

        return sorted(
            formatted,
            key=lambda x: (
                not x.get('is_us_production', False),
                x.get('rating_imdb') or x.get('rating_kp') or 0
            ),
            reverse=True
        )

    def _is_us_production(self, countries: List[Dict]) -> bool:
        if not countries:
            return False
        us_keywords = ['сша', 'америка', 'usa', 'united states', 'соединённые штаты']
        return any(country.get('name', '').lower() in us_keywords for country in countries)

    def _fallback_to_csv(self, genre_name: Optional[str], year: Optional[int], limit: int) -> List[Dict]:
        try:
            df = self._load_data_from_csv()
            filtered = df.copy()
            if genre_name:
                filtered = filtered[filtered['Genre'].str.contains(genre_name.lower(), na=False)]
            if year:
                filtered = filtered[filtered['Released_Year'] == year]
            if filtered.empty:
                return []
            sample = filtered.sample(min(limit, len(filtered)))
            records = sample.to_dict('records')
            for r in records:
                r.update({
                    'id': None,
                    'title': r.pop('Series_Title', '—'),
                    'genre': r.pop('Genre', '—').title(),
                    'country': 'США',
                    'rating_imdb': r.get('IMDB_Rating'),
                    'rating_kp': None,
                    'rating': r.get('IMDB_Rating', '—'),
                    'description': 'Описание недоступно в CSV.',
                    'is_us_production': True
                })
            return records
        except Exception as e:
            logger.error(f"Ошибка при работе с CSV: {e}")
            return []

    def _load_data_from_csv(self):
        df = pd.read_csv(self.data_path)
        df['Genre'] = df['Genre'].str.lower()
        df['Series_Title'] = df['Series_Title'].str.title()
        df['Released_Year'] = pd.to_numeric(df['Released_Year'], errors='coerce')
        return df

    def get_movie_by_id(self, movie_id: str) -> Optional[Dict]:
        if not self.use_api or not self.kinopoisk_client:
            return None
        try:
            movie_id_int = int(movie_id)
            details = self.kinopoisk_client.get_movie_details(movie_id_int)
            if details:
                rating_kp = details.get('rating', {}).get('kp')
                rating_imdb = details.get('rating', {}).get('imdb')
                genres_list = [g.get('name') for g in details.get('genres', []) if g.get('name')]
                countries_list = [c.get('name') for c in details.get('countries', []) if c.get('name')]
                return {
                    'id': details.get('id'),
                    'title': details.get('name') or 'Без названия',
                    'year': details.get('year'),
                    'genre': ', '.join(genres_list) if genres_list else '—',
                    'country': ', '.join(countries_list) if countries_list else '—',
                    'rating': rating_imdb or rating_kp or '—',
                    'rating_imdb': rating_imdb,
                    'rating_kp': rating_kp,
                    'description': (details.get('description') or 'Описание отсутствует.')[:500]
                }
        except Exception as e:
            logger.error(f"Ошибка получения фильма по ID {movie_id}: {e}", exc_info=True)
        return None

    def search_by_title(self, title: str) -> List[Dict]:
        if not self.use_api or not self.kinopoisk_client:
            return []
        try:
            base_url = f"{self.kinopoisk_client.base_url.split('/v1.4')[0]}/v1.4/movie"
            params = {
                'query': title,
                'limit': 10,
                'type': 'movie'
            }
            resp = self.kinopoisk_client.session.get(base_url, params=params, timeout=10)
            if resp.ok:
                data = resp.json()
                docs = data.get('docs', [])
                for movie in docs:
                    name = movie.get('name', '').lower()
                    alt_names = [n.lower() for n in movie.get('alternativeName', []) if n]
                    all_names = [name] + alt_names
                    if any(title.lower().strip() in n or n in title.lower().strip() for n in all_names):
                        genres = ', '.join([g['name'] for g in movie.get('genres', []) if g.get('name')])
                        countries = ', '.join([c['name'] for c in movie.get('countries', []) if c.get('name')])
                        rating_imdb = movie.get('rating', {}).get('imdb')
                        rating_kp = movie.get('rating', {}).get('kp')
                        return [{
                            'id': movie.get('id'),
                            'title': movie.get('name') or '—',
                            'year': movie.get('year'),
                            'genre': genres,
                            'country': countries,
                            'rating': rating_imdb or rating_kp or '—',
                            'rating_imdb': rating_imdb,
                            'rating_kp': rating_kp,
                            'description': (movie.get('description') or '')[:500]
                        }]
                if docs:
                    m = docs[0]
                    genres = ', '.join([g['name'] for g in m.get('genres', []) if g.get('name')])
                    countries = ', '.join([c['name'] for c in m.get('countries', []) if c.get('name')])
                    rating_imdb = m.get('rating', {}).get('imdb')
                    rating_kp = m.get('rating', {}).get('kp')
                    return [{
                        'id': m.get('id'),
                        'title': m.get('name') or '—',
                        'year': m.get('year'),
                        'genre': genres,
                        'country': countries,
                        'rating': rating_imdb or rating_kp or '—',
                        'rating_imdb': rating_imdb,
                        'rating_kp': rating_kp,
                        'description': (m.get('description') or '')[:500]
                    }]
            return []
        except Exception as e:
            logger.warning(f"Ошибка поиска по названию '{title}': {e}")
            return []