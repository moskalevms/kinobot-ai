# src/movie_agent.py
import os
import logging
from pathlib import Path
from typing import Optional, List, Dict, Union

import pandas as pd
from dotenv import load_dotenv

from src.client.kinopoisk_client import KinopoiskClient
from config import MIN_VOTES_IMDB, MIN_VOTES_KP

logger = logging.getLogger(__name__)
load_dotenv()


class MovieAgent:
    def __init__(self, use_api=True):
        self.use_api = use_api
        self.data_path = Path(__file__).parent.parent / "data" / "processed" / "imdb" / "imdb_top_1000.csv"
        self.kinopoisk_client = KinopoiskClient() if use_api else None

    def _load_data_from_csv(self):
        df = pd.read_csv(self.data_path)
        df['Genre'] = df['Genre'].str.lower()
        df['Series_Title'] = df['Series_Title'].str.title()
        df['Released_Year'] = pd.to_numeric(df['Released_Year'], errors='coerce')
        return df

    def recommend_movies(
            self,
            genre_name: Optional[str] = None,
            year: Optional[int] = None,
            actor: Optional[str] = None,
            director: Optional[str] = None,
            studio: Optional[str] = None,
            country: Optional[str] = None,
            min_imdb_rating: Optional[float] = None,
            limit: int = 5,
            movie_type: str = 'movie',
            query: Optional[str] = None
    ) -> Union[List[Dict], Dict]:
        try:
            if self.use_api and self.kinopoisk_client:
                effective_country = country if country else "США"

                # Запрашиваем с запасом: чтобы после фильтрации осталось хотя бы `limit`
                api_limit = max(limit * 4, 20)

                movies_data = self.kinopoisk_client.search_movies(
                    genre=genre_name,
                    year=year,
                    actor=actor,
                    imdb_rating_min=min_imdb_rating,
                    movie_type=movie_type,
                    query=query,
                    limit=api_limit
                )

                if not movies_data:
                    return []

                # Фильтрация по стране
                filtered_by_country = []
                for movie in movies_data['docs']:
                    countries = [c.get('name') for c in movie.get('countries', []) if c.get('name')]
                    if effective_country in countries:
                        filtered_by_country.append(movie)
                    elif effective_country == "США" and "Соединённые Штаты" in countries:
                        filtered_by_country.append(movie)

                final_list = filtered_by_country if filtered_by_country else movies_data['docs']

                # Преобразуем в единый формат, но не больше `limit`
                result = []
                for m in final_list[:limit]:
                    genres = ', '.join([g['name'] for g in m.get('genres', []) if g.get('name')])
                    countries = ', '.join([c['name'] for c in m.get('countries', []) if c.get('name')])
                    rating_imdb = m.get('rating', {}).get('imdb')
                    rating_kp = m.get('rating', {}).get('kp')
                    result.append({
                        'id': m.get('id'),
                        'title': m.get('name') or '—',
                        'year': m.get('year'),
                        'genre': genres,
                        'country': countries,
                        'rating': rating_imdb or rating_kp or '—',
                        'rating_imdb': rating_imdb,
                        'rating_kp': rating_kp,
                        'description': (m.get('description') or '')[:500]
                    })
                return result

            else:
                # fallback на CSV
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
                        'description': 'Описание недоступно в CSV.'
                    })
                return records

        except Exception as e:
            logger.error(f"Ошибка в recommend_movies: {e}", exc_info=True)
            return {"error": str(e)}

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

    # Используем search_movies с query + фильтрацией по типу "movie"
    # и дополнительной проверкой на точное совпадение названия
        try:
        # Сначала пробуем точный поиск через API
            base_url = f"{self.kinopoisk_client.base_url.split('/v1.4')[0]}/v1.4/movie"
            params = {
                'query': title,
                'limit': 10,  # запрашиваем больше, чтобы отфильтровать
                'type': 'movie'
            }
            resp = self.kinopoisk_client.session.get(base_url, params=params, timeout=10)
            if resp.ok:
                data = resp.json()
                docs = data.get('docs', [])

            # Ищем точное или близкое совпадение по названию (регистронезависимо)
                for movie in docs:
                    name = movie.get('name', '').lower()
                    alt_names = [n.lower() for n in movie.get('alternativeName', []) if n]
                    all_names = [name] + alt_names
                    if any(title.lower().strip() in n or n in title.lower().strip() for n in all_names):
                        # Нашли подходящий фильм
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

                # Если точного совпадения нет — возвращаем первый фильм (как fallback)
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
