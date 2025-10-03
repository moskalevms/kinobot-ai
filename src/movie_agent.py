import os
import logging
import math
from pathlib import Path
from typing import Optional, List, Dict, Union, Any
from dataclasses import dataclass

import pandas as pd
from dotenv import load_dotenv

from src.client.kinopoisk_client import KinopoiskClient

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
    limit: int = 10
    movie_type: str = 'movie'
    query: Optional[str] = None
    is_top_request: bool = False
    is_new_request: bool = False


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
            # Создаем поисковый запрос
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
                is_top_request=self._is_top_request(query),
                is_new_request=self._is_new_request(query, year)
            )

            # Многоуровневый поиск
            movies_data = self._multi_strategy_search(search_request)

            if not movies_data:
                return []

            # Преобразуем в единый формат с приоритетом для США
            result = self._format_movies(movies_data, search_request)

            logger.info(f"Найдено фильмов: {len(result)}")
            return result[:limit]

        except Exception as e:
            logger.error(f"Ошибка в recommend_movies: {e}", exc_info=True)
            return {"error": str(e)}

    def _multi_strategy_search(self, request: SearchRequest) -> List[Dict]:
        """Многоуровневый поиск фильмов"""
        all_movies = []

        # Стратегия 1: Поиск в топ-250 для качественных запросов
        if request.is_top_request or not request.genre:
            top_movies = self.kinopoisk_client.search_top250(
                genre=request.genre,
                year=request.year,
                country=request.country,
                limit=request.limit
            )
            if top_movies and top_movies.get('docs'):
                all_movies.extend(top_movies['docs'])

        # Стратегия 2: Обычный поиск по рейтингу
        if len(all_movies) < request.limit:
            regular_movies = self.kinopoisk_client.search_movies(
                genre=request.genre,
                year=request.year,
                actor=request.actor,
                imdb_rating_min=request.min_rating,
                movie_type=request.movie_type,
                query=request.query,
                limit=request.limit * 2,  # Берем с запасом
                country=request.country
            )
            if regular_movies and regular_movies.get('docs'):
                # Добавляем только новые фильмы
                existing_ids = {m['id'] for m in all_movies}
                for movie in regular_movies['docs']:
                    if movie['id'] not in existing_ids:
                        all_movies.append(movie)

        return all_movies

    def _format_movies(self, movies_data: List[Dict], request: SearchRequest) -> List[Dict]:
        """Форматирование результатов с приоритетом для США"""
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

        # Сортируем: сначала фильмы из США, затем по рейтингу
        return sorted(formatted,
                      key=lambda x: (
                          not x.get('is_us_production', False),  # США сначала
                          x.get('rating_imdb') or x.get('rating_kp') or 0  # затем по рейтингу
                      ),
                      reverse=True)

    def _is_us_production(self, countries: List[Dict]) -> bool:
        """Проверяет, является ли фильм американским"""
        if not countries:
            return False
        us_keywords = ['сша', 'америка', 'usa', 'united states', 'соединённые штаты']
        return any(country.get('name', '').lower() in us_keywords for country in countries)

    def _is_top_request(self, query: Optional[str]) -> bool:
        """Определяет, является ли запрос поиском лучших фильмов"""
        if not query:
            return False
        top_keywords = ['лучшие', 'топ', 'рейтинг', 'лучший', 'топ10', 'топ 10']
        return any(keyword in query.lower() for keyword in top_keywords)

    def _is_new_request(self, query: Optional[str], year: Optional[int]) -> bool:
        """Определяет, является ли запрос поиском новых фильмов"""
        if year and year >= 2023:
            return True
        if not query:
            return False
        new_keywords = ['новые', 'свежие', 'недавние', '2024', '2025']
        return any(keyword in query.lower() for keyword in new_keywords)

    def _fallback_to_csv(self, genre_name: Optional[str], year: Optional[int], limit: int) -> List[Dict]:
        """Fallback на CSV данные"""
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