# src/movie_agent.py
import logging
import time
from typing import List, Dict, Optional, Tuple
from kinopoisk_client import KinopoiskClient
from recommendation_engine import RecommendationEngine
from config import KINOPOISK_API_KEY, CACHE_TTL, CURRENT_YEAR
from utils.movie_filter import extract_imdb_id
from rt_enrichment import enrich_movies_with_rt_scores

logger = logging.getLogger(__name__)
CANDIDATE_LIMIT = 150


class MovieAgent:
    def __init__(self, use_api: bool = True):
        self.use_api = use_api
        self.kinopoisk_client = KinopoiskClient(api_key=KINOPOISK_API_KEY)
        self.recommendation_engine = RecommendationEngine(self.kinopoisk_client)
        self._search_cache: Dict[str, Tuple[List[Dict], float]] = {}

    def _get_cache_key(self, user_id, genre_name, year, year_range, actor, director, country, min_imdb_rating, limit, movie_type, query,
                       critics_approved=False) -> str:
        parts = [
            user_id or '',
            genre_name or '',
            str(year),
            str(year_range),
            actor or '',
            director or '',
            country or '',
            str(min_imdb_rating),
            str(limit),
            movie_type,
            query or '',
            # Режим «одобрено критиками» меняет выдачу — учитываем в ключе,
            # иначе кэш смешает обычный и критический подбор
            str(bool(critics_approved))
        ]
        return '_'.join(parts)

    async def recommend_movies(
        self,
        session,
        genre_name: Optional[str] = None,
        year: Optional[int] = None,
        year_range: Optional[tuple] = None,
        actor: Optional[str] = None,
        director: Optional[str] = None,
        country: Optional[str] = None,
        min_imdb_rating: float = 6.5,
        limit: int = 8,
        movie_type: str = 'movie',
        query: Optional[str] = None,
        user_id: Optional[str] = None,
        critics_approved: bool = False
    ) -> List[Dict]:
        current_year = CURRENT_YEAR
        if year_range and year_range[1] > current_year:
            year_range = (year_range[0], current_year)
            logger.info(f"Корректировка year_range на текущий год: {year_range}")

        cache_key = self._get_cache_key(user_id, genre_name, year, year_range, actor, director, country, min_imdb_rating, limit, movie_type, query,
                                        critics_approved)

        if cache_key in self._search_cache:
            cached_data, timestamp = self._search_cache[cache_key]
            if time.time() - timestamp < CACHE_TTL:
                logger.info("[MovieAgent] Кэш HIT")
                return cached_data
            else:
                del self._search_cache[cache_key]

        is_top = any(word in (query or '').lower() for word in ['топ', 'лучш', 'рейтинг', 'best', 'top']) if query else False

        try:
            movies = await self.recommendation_engine.get_recommendations(
                session=session,
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
                critics_approved=critics_approved
            )
            if movies:
                self._search_cache[cache_key] = (movies, time.time())
            else:
                logger.info("[MovieAgent] Поиск не дал результатов")
            logger.info(f"Найдено {movie_type}: {len(movies)}")
            return movies
        except Exception as e:
            logger.error(f"Ошибка в recommend_movies: {e}", exc_info=True)
            return []

    async def search_by_title(self, session, title: str) -> List[Dict]:
        try:
            data = await self.kinopoisk_client.search_movie_by_title(session, title, limit=10)
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

            card = {
                'id': best_match.get('id'),
                'title': best_match.get('name') or '—',
                'year': best_match.get('year'),
                'genre': genres,
                'country': countries,
                'rating': rating_imdb or rating_kp or '—',
                'rating_imdb': rating_imdb,
                'rating_kp': rating_kp,
                # Описание НЕ режется «на глаз» (прежний срез [:500] убран):
                # бюджет caption (1024) обеспечивает единая сборка карточки
                # `build_movie_card`, общая для info-интента и callback `info:`.
                # Без этого текст карточки одного фильма в двух путях разъезжался
                # (прямое требование A4 / дельта-спека movie-card-presentation).
                'description': best_match.get('description') or '',
                'poster_url': poster_url,
                # Ссылка на Кинопоиск строится ПОБАЙТОВО как в движке выдачи
                # (recommendation_engine.py) и в callback-пути (telegram_bot):
                # без неё у карточки info-интента не было бы кнопки «🔗 Кинопоиск».
                'kinopoisk_url': f"https://www.kinopoisk.ru/film/{best_match.get('id')}/" if best_match.get('id') else None,
                # IMDb ID из externalId (фаза 1, B3) — join-ключ для RT (B5)
                'imdb_id': extract_imdb_id(best_match)
            }
            # Обогащение карточки RT-оценками (фаза 1, B5): одно обращение к
            # источнику, данные для бейджа B7 в info-интенте. Fail-silent —
            # при любом сбое карточка возвращается с rt_score=None.
            return await enrich_movies_with_rt_scores(session, [card])
        except Exception as e:
            logger.warning(f"Ошибка поиска по названию '{title}': {e}", exc_info=True)
            return []

    async def health_check(self, session) -> bool:
        try:
            test_movies = await self.recommend_movies(
                session, genre_name='комедия', limit=1, user_id='health'
            )
            return bool(test_movies)
        except Exception:
            return False

    def clear_cache(self, user_id: Optional[str] = None):
        """Очистить кэш поиска: только для пользователя, если задан user_id"""
        if user_id is None:
            self._search_cache.clear()
            logger.info("[MovieAgent] Кэш очищен для всех пользователей")
        else:
            prefix = f"{user_id}_"
            keys = [k for k in self._search_cache if k.startswith(prefix)]
            for key in keys:
                del self._search_cache[key]
            logger.info(f"[MovieAgent] Кэш очищен для пользователя {user_id}: {len(keys)} записей")
