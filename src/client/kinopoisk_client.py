# src/client/kinopoisk_client.py
import logging
import requests
from typing import Optional
from config import KINOPOISK_API_KEY, KINOPOISK_URL, MIN_VOTES_IMDB, MIN_VOTES_KP

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
        limit: int = 50
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
        if imdb_rating_min is not None:
            params['rating.imdb'] = str(imdb_rating_min)
        if kp_rating_min is not None:
            params['rating.kp'] = str(kp_rating_min)

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

            filtered_docs = []
            for movie in raw_docs:
                rating = movie.get('rating', {})
                votes = movie.get('votes', {})

                # Используем значения из config
                imdb_val = rating.get('imdb')
                imdb_votes = votes.get('imdb', 0)
                kp_val = rating.get('kp')
                kp_votes = votes.get('kp', 0)

                imdb_ok = (imdb_rating_min is None) or (imdb_val is not None and imdb_val >= imdb_rating_min)
                kp_ok = (kp_rating_min is None) or (kp_val is not None and kp_val >= kp_rating_min)

                # Проверка по количеству голосов
                has_enough_imdb = imdb_votes >= MIN_VOTES_IMDB
                has_enough_kp = kp_votes >= MIN_VOTES_KP

                # Фильм проходит, если:
                # - IMDb рейтинг надёжный И удовлетворяет min_rating, ИЛИ
                # - KP рейтинг надёжный И удовлетворяет min_rating
                passes_imdb = imdb_ok and has_enough_imdb
                passes_kp = kp_ok and has_enough_kp

                if passes_imdb or passes_kp:
                    filtered_docs.append(movie)

            logger.info(f"[KinopoiskClient] После фильтрации по голосам осталось: {len(filtered_docs)} фильмов")

            if not filtered_docs:
                logger.info("[KinopoiskClient] Все фильмы отфильтрованы — ни один не прошёл порог голосов")
                return None

            result_docs = filtered_docs[:limit]
            logger.info(f"[KinopoiskClient] Возвращаем {len(result_docs)} фильмов")
            data['docs'] = result_docs
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