# src/kinopoisk_client.py
import aiohttp
import logging
import asyncio
from typing import Optional, List, Dict, Tuple
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from config import KINOPOISK_URL

logger = logging.getLogger(__name__)

def is_kinopoisk_transient_error(exception):
    if isinstance(exception, aiohttp.ClientError):
        return True
    if "HTTP" in str(exception) and any(code in str(exception) for code in ["429", "500", "502", "503", "504"]):
        return True
    return False

class KinopoiskClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = f"{KINOPOISK_URL}/v1.4/movie"
        self.headers = {
            'X-API-KEY': self.api_key,
            'Accept': 'application/json'
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        before_sleep=lambda retry_state: logger.warning(f"[Kinopoisk] Повторная попытка {retry_state.attempt_number}/3 для запроса: {retry_state.args}"),
        reraise=True
    )
    async def _make_request(self, session: aiohttp.ClientSession, url: str, params: dict) -> Optional[dict]:
        try:
            async with session.get(
                url,
                params=params,
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logger.error(f"[Kinopoisk] HTTP {resp.status}: {await resp.text()}")
                    # Повторяем только для 429/5xx
                    if resp.status in (429, 500, 502, 503, 504):
                        raise aiohttp.ClientResponseError(
                            request_info=resp.request_info,
                            history=resp.history,
                            status=resp.status,
                            message=f"HTTP {resp.status}",
                            headers=resp.headers
                        )
                    return None
        except Exception as e:
            logger.error(f"[Kinopoisk] Ошибка запроса: {e}")
            raise

    async def search_movies(
        self,
        session: aiohttp.ClientSession,
        genre: Optional[str] = None,
        year: Optional[int] = None,
        year_range: Optional[Tuple[int, int]] = None,
        actor: Optional[str] = None,
        director: Optional[str] = None,
        imdb_rating_min: Optional[float] = None,
        kp_rating_min: Optional[float] = None,
        movie_type: str = 'movie',
        query: Optional[str] = None,
        limit: int = 100,
        country: Optional[str] = None,
        sort_by: str = 'rating.imdb'
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
            'type': movie_type
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
            person = await self.search_person_by_name(session, actor)
            if person:
                params['persons.id'] = person['id']
            else:
                logger.warning(f"Актёр '{actor}' не найден.")
        if director:
            person = await self.search_person_by_name(session, director)
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
        return await self._make_request(session, self.base_url, params)

    async def search_recommendation(
        self,
        session: aiohttp.ClientSession,
        genre: Optional[str] = None,
        year: Optional[int] = None,
        country: Optional[str] = None,
        limit: int = 100,
        movie_type: str = 'movie'
    ) -> Optional[dict]:
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
        return await self._make_request(session, self.base_url, params)

    async def search_person_by_name(self, session: aiohttp.ClientSession, name: str) -> Optional[Dict]:
        url = f"{KINOPOISK_URL}/v1.4/person/search"
        params = {'query': name, 'limit': 1}
        try:
            async with session.get(url, params=params, headers=self.headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    docs = data.get('docs', [])
                    if docs:
                        person = docs[0]
                        logger.info(f"[KinopoiskClient] Найдена персона: {person.get('name')} (ID: {person.get('id')})")
                        return person
                    logger.warning(f"[KinopoiskClient] Персона '{name}' не найдена.")
                    return None
                else:
                    logger.error(f"[Kinopoisk] Поиск персоны — HTTP {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"[KinopoiskClient] Ошибка поиска персоны '{name}': {e}")
            return None

    async def search_movie_by_title(self, session: aiohttp.ClientSession, title: str, limit: int = 5) -> Optional[dict]:
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
        return await self._make_request(session, url, params)