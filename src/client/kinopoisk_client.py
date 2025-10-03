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
            limit: int = 50,
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

            # Новая логика фильтрации с динамическими порогами
            filtered_docs = self._filter_movies_by_quality(raw_docs, year)
            logger.info(f"[KinopoiskClient] После фильтрации по качеству: {len(filtered_docs)} фильмов")

            if not filtered_docs:
                logger.info("[KinopoiskClient] Все фильмы отфильтрованы")
                return None

            result_docs = filtered_docs[:limit]
            logger.info(f"[KinopoiskClient] Возвращаем {len(result_docs)} фильмов")
            data['docs'] = result_docs
            return data

        except Exception as e:
            logger.error(f"[KinopoiskClient] Ошибка поиска фильмов: {e}")
            return None

    def _filter_movies_by_quality(self, movies: List[Dict], year: Optional[int] = None) -> List[Dict]:
        """Фильтрация фильмов по качеству с динамическими порогами"""
        filtered = []

        for movie in movies:
            rating = movie.get('rating', {})
            votes = movie.get('votes', {})

            # Получаем рейтинги
            imdb_rating = rating.get('imdb')
            kp_rating = rating.get('kp')

            # Получаем голоса
            imdb_votes = votes.get('imdb', 0)
            kp_votes = votes.get('kp', 0)

            # Определяем лучший рейтинг (IMDB приоритетнее)
            best_rating = imdb_rating if imdb_rating else kp_rating
            best_votes = imdb_votes if imdb_rating else kp_votes

            if not best_rating or best_rating < 6.0:
                continue

            # Динамические пороги голосов в зависимости от года
            min_votes = self._calculate_min_votes(year)
            if best_votes < min_votes:
                continue

            # Проверка наличия основных данных
            if not movie.get('name') or not movie.get('year'):
                continue

            filtered.append(movie)

        return filtered

    def _calculate_min_votes(self, year: Optional[int] = None) -> int:
        """Динамический расчет минимального количества голосов"""
        current_year = 2025

        if not year:
            return 10000  # Для фильмов без указания года

        year_diff = current_year - year

        if year_diff <= 1:  # Новинки (2024-2025)
            return 1000
        elif year_diff <= 3:  # Недавние (2022-2023)
            return 5000
        elif year_diff <= 10:  # Современные (2015-2021)
            return 10000
        else:  # Классика (до 2015)
            return 20000

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
                      country: Optional[str] = None, limit: int = 20) -> Optional[dict]:
        """Поиск в топ-250 фильмах"""
        params = {
            'lists': 'top250',
            'limit': limit * 2,  # Берем с запасом для фильтрации
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
            data = response.json()

            # Фильтруем по качеству
            filtered_docs = self._filter_movies_by_quality(data.get('docs', []), year)
            data['docs'] = filtered_docs[:limit]

            logger.info(f"[KinopoiskClient] Найдено в топ250: {len(filtered_docs)} фильмов")
            return data

        except Exception as e:
            logger.error(f"Ошибка поиска в топ250: {e}")
            return None