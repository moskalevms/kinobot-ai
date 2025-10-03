import logging
import requests
from typing import Optional, List, Dict, Any
from config import KINOPOISK_API_KEY, KINOPOISK_URL
from src.models.movie import Movie, MovieRating, MovieVotes, Genre, Country

logger = logging.getLogger(__name__)


class KinopoiskClient:
    def __init__(self):
        self.api_key = KINOPOISK_API_KEY
        self.base_url = f"{KINOPOISK_URL.rstrip('/')}/v1.4/movie"
        self.person_search_url = f"{KINOPOISK_URL.rstrip('/')}/v1.4/person/search"
        self.session = requests.Session()
        self.session.headers.update({
            'X-API-KEY': self.api_key,
            'Content-Type': 'application/json'
        })

    def search_person_by_name(self, name: str) -> Optional[dict]:
        params = {'query': name, 'limit': 1}
        try:
            response = self.session.get(self.person_search_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            docs = data.get('docs', [])
            if docs:
                person = docs[0]
                return {'id': person['id'], 'name': person['name']}
            else:
                logger.warning(f"Персона не найдена: '{name}'")
                return None
        except Exception as e:
            logger.error(f"Ошибка поиска персоны '{name}': {e}")
            return None

    def search_movies(
            self,
            genre: Optional[str] = None,
            year: Optional[int] = None,
            actor: Optional[str] = None,
            imdb_rating_min: Optional[float] = None,
            kp_rating_min: Optional[float] = None,
            movie_type: str = 'movie',
            query: Optional[str] = None,
            limit: int = 100,
            country: Optional[str] = None
    ) -> Optional[dict]:
        params = {
            'limit': min(limit, 250),
            'page': 1,
            'selectFields': [
                'id', 'name', 'year', 'genres', 'rating', 'votes',
                'description', 'poster', 'persons', 'countries', 'type'
            ],
            'sortField': 'rating.imdb',
            'sortType': -1,
            'type': movie_type
        }

        if query:
            params['query'] = query
        if year:
            params['year'] = year
        if genre:
            params['genres.name'] = genre
        if actor:
            person = self.search_person_by_name(actor)
            if person:
                params['persons.id'] = person['id']
            else:
                logger.warning(f"Актёр '{actor}' не найден.")
        if country:
            params['countries.name'] = country
        if imdb_rating_min is not None:
            params['rating.imdb'] = f"{imdb_rating_min}-10"
        if kp_rating_min is not None:
            params['rating.kp'] = f"{kp_rating_min}-10"

        logger.info(f"[KinopoiskClient] Запрос: {params}")

        try:
            response = self.session.get(self.base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            raw_docs = data.get('docs', [])

            logger.info(f"[KinopoiskClient] Получено от API: {len(raw_docs)} фильмов")

            if not raw_docs:
                logger.info("[KinopoiskClient] API вернул пустой результат")
                return None

            # ⚠️ Фильтрация теперь делается ВНЕ этого класса!
            data['docs'] = raw_docs
            return data

        except Exception as e:
            logger.error(f"[KinopoiskClient] Ошибка поиска фильмов: {e}")
            return None

    def get_movie_details(self, movie_id: int) -> Optional[dict]:
        url = f"{self.base_url}/{movie_id}"
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Ошибка деталей фильма {movie_id}: {e}")
            return None

    def search_top250(self, genre: Optional[str] = None, year: Optional[int] = None,
                      country: Optional[str] = None, limit: int = 100) -> Optional[dict]:
        params = {
            'lists': 'top250',
            'limit': min(limit, 250),
            'selectFields': [
                'id', 'name', 'year', 'genres', 'rating', 'votes',
                'description', 'poster', 'countries'
            ],
            'sortField': 'rating.imdb',
            'sortType': -1
        }

        if genre:
            params['genres.name'] = genre
        if year:
            params['year'] = year
        if country:
            params['countries.name'] = country

        try:
            response = self.session.get(self.base_url, params=params, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Ошибка поиска в топ250: {e}")
            return None