# src/recommendation_engine.py
import logging
from typing import List, Dict, Optional, Tuple, Set
from utils.movie_filter import (
    EXCLUDED_GENRES,
    MUSIC_ONLY_GENRES,
    extract_critics_fields,
    extract_imdb_id,
    filter_movies_by_quality,
    get_country_priority,
    get_critics_tier,
    get_weighted_rating,
    is_russian_content,
    rerank_with_rt,
)
from kinopoisk_client import KinopoiskClient
from rt_enrichment import enrich_movies_with_rt_scores

logger = logging.getLogger(__name__)

# Синонимы стендапа в запросах. Отдельного жанра «стендап» у Кинопоиска
# нет, поэтому при явном запросе поиск идёт по «комедии», а псевдожанр
# «стендап» в allowed_excluded_genres снимает исключение стендапов.
STANDUP_SYNONYMS = ('стендап', 'стенд-ап', 'стенд ап', 'stand-up', 'standup')

# Локальные ворота режима «одобрено критиками» (фаза 0, Epic A, A3):
# диапазонный фильтр rating.filmCritics=7-10 через query-параметры API
# молча игнорируется (подтверждённый баг kinopoisk.dev), поэтому
# фильтрация по самой оценке — только локальная. Порог голосов —
# отдельная константа (не переиспользуем CRITICS_MIN_VOTES_SHRINK из
# A1): тюнинг шринка в фазе 2 не должен сдвигать ворота триггера.
CRITICS_APPROVED_MIN_RATING = 7.0
CRITICS_APPROVED_MIN_VOTES = 10


def _is_critics_approved(movie: Dict) -> bool:
    """Локальная проверка «одобрено критиками»: fc >= 7 и голосов >= 10.

    Отсеивает мусор вида «10.0 при 1–2 рецензиях». Число голосов
    дополнительно отсекается на стороне API (votes.filmCritics=10-100000),
    но локальная проверка страхует от багов источника и данных, пришедших
    из других контуров поиска.
    """
    critics_rating, critics_votes = extract_critics_fields(movie)
    if critics_rating is None or critics_votes is None:
        return False
    return (
        critics_rating >= CRITICS_APPROVED_MIN_RATING
        and critics_votes >= CRITICS_APPROVED_MIN_VOTES
    )


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
        is_top: bool = False,
        critics_approved: bool = False
    ) -> List[Dict]:
        allowed_excluded_genres = set()
        is_russian_search = bool(country and country.lower() in ['россия', 'russia', 'российская федерация'])
        if genre_name and genre_name.lower() in EXCLUDED_GENRES:
            allowed_excluded_genres.add(genre_name.lower())
        if genre_name and genre_name.lower() in MUSIC_ONLY_GENRES:
            allowed_excluded_genres.add(genre_name.lower())
        if genre_name and genre_name.lower() in STANDUP_SYNONYMS:
            allowed_excluded_genres.add('стендап')
            genre_name = 'комедия'
        if query:
            query_lower = query.lower()
            for excluded_genre in EXCLUDED_GENRES:
                if excluded_genre in query_lower:
                    allowed_excluded_genres.add(excluded_genre)
            if 'музык' in query_lower:
                allowed_excluded_genres.add('музыка')
            if any(synonym in query_lower for synonym in STANDUP_SYNONYMS):
                allowed_excluded_genres.add('стендап')
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
                is_russian_search=is_russian_search,
                critics_approved=critics_approved
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
                is_russian_search=is_russian_search,
                critics_approved=critics_approved
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
        allowed_excluded_genres: Optional[Set[str]] = None,
        is_russian_search: bool = False,
        critics_approved: bool = False
    ) -> List[Dict]:
        if allowed_excluded_genres is None:
            allowed_excluded_genres = set()
        candidates: List[Dict] = []
        min_votes_override = None
        actual_genre = genre_name

        if genre_name == 'аниме':
            search_types = ['anime', 'tv-series', 'movie']
        else:
            search_types = [movie_type]

        for search_type in search_types:
            if len(candidates) >= limit * 2:
                break

            # 🔧 ИСПРАВЛЕНИЕ: top250 НЕ ИСПОЛЬЗУЕТСЯ при year_range.
            # В режиме «одобрено критиками» top250 тоже пропускаем: список
            # не основан на критиках и был бы целиком отсеян локальным
            # порогом fc >= 7 — не тратим запрос к API.
            if is_top and year_range is None and not critics_approved:
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
                country=country,
                critics_approved=critics_approved
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

        if critics_approved:
            # Порог по оценке критиков через query-параметры API не работает
            # (подтверждённый баг kinopoisk.dev) — применяем только локально
            unique_candidates = [m for m in unique_candidates if _is_critics_approved(m)]
            logger.info(
                f"[RecommendationEngine] Режим «одобрено критиками»: "
                f"после локального порога fc>={CRITICS_APPROVED_MIN_RATING} "
                f"осталось {len(unique_candidates)} кандидатов"
            )

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

        if critics_approved:
            # Фильтр качества сортирует по взвешенной оценке; в режиме
            # «одобрено критиками» итоговый порядок — по убыванию fc
            filtered.sort(key=lambda m: extract_critics_fields(m)[0] or 0.0, reverse=True)

        # Обогащение RT-оценками (Epic B, B5) — только финальный список
        # (после фильтра качества и обрезки до limit), не кандидаты (до 250)
        enriched = await enrich_movies_with_rt_scores(
            session, self._format_movies_list(filtered, limit, is_russian_search)
        )
        if not critics_approved:
            # B6: пересортировка финала по оценке с учётом Tomatometer.
            # В режиме «одобрено критиками» контракт порядка — убывание fc
            # (A3), поэтому RT-пересортировка там не применяется.
            rerank_with_rt(enriched, is_russian_search)
        return enriched

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
        allowed_excluded_genres: Optional[Set[str]] = None,
        is_russian_search: bool = False,
        critics_approved: bool = False
    ) -> List[Dict]:
        if allowed_excluded_genres is None:
            allowed_excluded_genres = set()
        candidates: List[Dict] = []
        actual_genre = genre_name

        if genre_name == 'аниме':
            search_types = ['anime', 'tv-series', 'movie']
        else:
            search_types = [movie_type]

        for search_type in search_types:
            if len(candidates) >= limit * 2:
                break

            # Здесь year_range всегда None, поэтому top250 можно использовать.
            # В режиме «одобрено критиками» top250 пропускаем: список не
            # основан на критиках и был бы отсеян локальным порогом fc >= 7.
            if is_top and not critics_approved:
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
                limit=250,
                critics_approved=critics_approved
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

        if critics_approved:
            # Порог по оценке критиков через query-параметры API не работает
            # (подтверждённый баг kinopoisk.dev) — применяем только локально
            unique_candidates = [m for m in unique_candidates if _is_critics_approved(m)]
            logger.info(
                f"[RecommendationEngine] Режим «одобрено критиками»: "
                f"после локального порога fc>={CRITICS_APPROVED_MIN_RATING} "
                f"осталось {len(unique_candidates)} кандидатов"
            )

        filtered = filter_movies_by_quality(
            unique_candidates,
            min_rating=min_imdb_rating,
            exclude_anime=(actual_genre != 'аниме'),
            prioritize_english_speaking=not is_russian_search,
            allowed_excluded_genres=allowed_excluded_genres,
            is_russian_search=is_russian_search
        )

        if critics_approved:
            # Фильтр качества сортирует по взвешенной оценке; в режиме
            # «одобрено критиками» итоговый порядок — по убыванию fc
            filtered.sort(key=lambda m: extract_critics_fields(m)[0] or 0.0, reverse=True)

        # Обогащение RT-оценками (Epic B, B5) — только финальный список
        # (после фильтра качества и обрезки до limit), не кандидаты (до 250)
        enriched = await enrich_movies_with_rt_scores(
            session, self._format_movies_list(filtered, limit, is_russian_search)
        )
        if not critics_approved:
            # B6: пересортировка финала по оценке с учётом Tomatometer.
            # В режиме «одобрено критиками» контракт порядка — убывание fc
            # (A3), поэтому RT-пересортировка там не применяется.
            rerank_with_rt(enriched, is_russian_search)
        return enriched

    def _format_movies_list(self, movies: List[Dict], limit: int, is_russian_search: bool = False) -> List[Dict]:
        formatted: List[Dict] = []
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

            # Поля кинокритиков (фаза 0, Epic A, A2): присутствуют при
            # наличии данных в ответе API, иначе None. В UI пока не
            # выводятся — данные для триггеров (A3), ранжирования (B6)
            # и отладки; семантика «нет данных» согласована с A1.
            critics_rating, critics_votes = extract_critics_fields(movie)

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
                'critics_rating': critics_rating,
                'critics_votes': critics_votes,
                # IMDb ID из externalId (фаза 1, B3): join-ключ для
                # обогащения RT-скорами (B5); None у ~31% фильмов.
                'imdb_id': extract_imdb_id(movie),
                # Внутренние поля для пересортировки финала с учётом RT
                # (фаза 1, B6): оценка s1 после критиков (A1), величина
                # ступени fc для суммарного клампа ступеней и группа
                # странового приоритета. В UI не выводятся.
                'weighted_score': get_weighted_rating(movie, is_russian_search),
                'critics_tier': get_critics_tier(movie),
                'country_priority': get_country_priority(movie),
                'description': description,
                'poster_url': poster_url,
                'kinopoisk_url': f"https://www.kinopoisk.ru/film/{movie.get('id')}/" if movie.get('id') else None,
                'type': movie.get('type', 'movie')
            }
            formatted.append(formatted_movie)

        logger.info(f"[RecommendationEngine] Отформатировано фильмов: {len(formatted)} из запрошенных {limit}")
        return formatted
