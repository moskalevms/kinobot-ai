# src/kinopoisk_client.py
import requests
import logging
from typing import Optional, List, Dict, Tuple
from .config import KINOPOISK_URL

logger = logging.getLogger(__name__)

class KinopoiskClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = f"{KINOPOISK_URL}/v1.4/movie"
        self.session = requests.Session()
        self.session.headers.update({
            'X-API-KEY': self.api_key,
            'Accept': 'application/json'
        })

    def search_movies(
            self,
            genre: Optional[str] = None,
            year: Optional[int] = None,
            year_range: Optional[Tuple[int, int]] = None,
            actor: Optional[str] = None,
            director: Optional[str] = None,
            imdb_rating_min: Optional[float] = None,
            kp_rating_min: Optional[float] = None,
            movie_type: str = 'movie',  # ← добавлено: по умолчанию movie, но поддержка tv-series
            query: Optional[str] = None,
            limit: int = 100,
            country: Optional[str] = None,
            sort_by: str = 'rating.imdb'  # 'rating.imdb', 'votes.imdb', etc.
    ) -> Optional[dict]:
        params = {
            'limit': min(limit, 250),
            'page': 1,
            'selectFields': [
                'id', 'name', 'year', 'genres', 'rating', 'votes',
                'description', 'poster', 'persons', 'countries', 'type'
            ],
            'sortField': sort_by,
            'sortType': -1,
            'type': movie_type  # ← передаём movie_type
        }
        if year_range:
            params['year'] = f"{year_range[0]}-{year_range[1]}"
        elif year:
            params['year'] = year
        if query:
            params['query'] = query
        if genre:
            params['genres.name'] = genre
        if actor:
            person = self.search_person_by_name(actor)
            if person:
                params['persons.id'] = person['id']
            else:
                logger.warning(f"Актёр '{actor}' не найден.")
        if director:
            person = self.search_person_by_name(director)
            if person:
                params['persons.id'] = person['id']
            else:
                logger.warning(f"Режиссёр '{director}' не найден.")
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
            logger.info(f"[KinopoiskClient] Получено от API: {len(raw_docs)} {movie_type}")
            if not raw_docs:
                logger.info("[KinopoiskClient] API вернул пустой результат")
                return None
            data['docs'] = raw_docs
            return data
        except requests.exceptions.HTTPError as e:
            logger.error(f"[KinopoiskClient] Ошибка HTTP: {e}, ответ: {response.text}")
            return None
        except Exception as e:
            logger.error(f"[KinopoiskClient] Ошибка поиска {movie_type}: {e}")
            return None

    def search_recommendation(self, genre: Optional[str] = None, year: Optional[int] = None,  # ← фикс опечатки: search_recommendation
                               country: Optional[str] = None, limit: int = 100, movie_type: str = 'movie') -> Optional[dict]:  # ← добавлено movie_type
        params = {
            'lists': 'top250',
            'limit': min(limit, 250),
            'selectFields': [
                'id', 'name', 'year', 'genres', 'rating', 'votes',
                'description', 'poster', 'countries'
            ],
            'sortField': 'rating.imdb',
            'sortType': -1,
            'type': movie_type
        }
        if genre:
            params['genres.name'] = genre
        if year:
            params['year'] = year
        if country:
            params['countries.name'] = country

        logger.info(f"[KinopoiskClient] Запрос рекомендаций {movie_type}: {params}")
        try:
            response = self.session.get(self.base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            raw_docs = data.get('docs', [])
            logger.info(f"[KinopoiskClient] Получено рекомендованных {movie_type}: {len(raw_docs)}")
            return data
        except requests.exceptions.HTTPError as e:
            logger.error(f"[KinopoiskClient] Ошибка HTTP в top250: {e}, ответ: {response.text}")
            return None
        except Exception as e:
            logger.error(f"[KinopoiskClient] Ошибка в top250 {movie_type}: {e}")
            return None

    def search_person_by_name(self, name: str) -> Optional[Dict]:
        url = f"{KINOPOISK_URL}/v1.4/person/search"
        params = {
            'query': name,
            'limit': 1
        }
        try:
            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            docs = data.get('docs', [])
            if docs:
                person = docs[0]
                logger.info(f"[KinopoiskClient] Найдена персона: {person.get('name')} (ID: {person.get('id')})")
                return person
            logger.warning(f"[KinopoiskClient] Персона '{name}' не найдена.")
            return None
        except requests.exceptions.HTTPError as e:
            logger.error(f"[KinopoiskClient] Ошибка HTTP при поиске персоны '{name}': {e}, ответ: {response.text}")
            return None
        except Exception as e:
            logger.error(f"[KinopoiskClient] Ошибка поиска персоны '{name}': {e}")
            return None

    def search_movie_by_title(self, title: str, limit: int = 5) -> Optional[dict]:
        """
        Точный поиск фильма по названию через /v1.4/movie/search
        """
        url = f"{self.base_url}/search"
        params = {
            'query': title,
            'limit': min(limit, 20),
            'selectFields': [
                'id', 'name', 'alternativeName', 'year', 'genres', 'rating',
                'votes', 'description', 'poster', 'countries', 'type'
            ]
        }
        logger.info(f"[KinopoiskClient] Поиск по названию: {title}")
        try:
            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            docs = data.get('docs', [])
            logger.info(f"[KinopoiskClient] Найдено по названию: {len(docs)}")
            return data
        except Exception as e:
            logger.error(f"[KinopoiskClient] Ошибка поиска по названию '{title}': {e}")
            return None