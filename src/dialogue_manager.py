# src/dialogue_manager.py
import os
import logging

from typing import Dict, Any, List, Optional, Tuple
from movie_agent import MovieAgent
from session_manager import SessionManager, UserSession
from intent_classifier import IntentClassifier
from llm_router import LLMRouter
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)


class DialogueManager:
    def __init__(self, session_manager: SessionManager):
        self.session_manager = session_manager
        self.movie_agent = MovieAgent(use_api=True)
        self.llm_router = LLMRouter()
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.prompts_dir = os.path.join(current_dir, 'prompts')
        logger.info(f"Prompts directory: {self.prompts_dir}")
        if not os.path.exists(self.prompts_dir):
            os.makedirs(self.prompts_dir, exist_ok=True)
        self.intent_classifier = IntentClassifier(self.llm_router, self.prompts_dir)

        self.mood_to_genre = {
            'грустн': ['комедия', 'мультфильм', 'мюзикл', 'романтическая комедия'],
            'весел': ['комедия', 'приключения', 'фэнтези', 'семейный'],
            'устал': ['мелодрама', 'драма', 'семейный', 'приключения', 'исторический'],
            'скучно': ['боевик', 'триллер', 'приключения', 'фантастика', 'детектив'],
            'страшн': ['ужасы', 'триллер', 'мистика', 'психологический триллер'],
            'романт': ['мелодрама', 'романтическая комедия', 'драма'],
            'адреналин': ['боевик', 'триллер', 'приключения', 'военный', 'фантастика'],
            'умный': ['драма', 'исторический', 'психологический']
        }

    def _load_prompt(self, filename: str) -> str:
        path = os.path.join(self.prompts_dir, filename)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception as e:
            logger.error(f"Ошибка загрузки промпта {filename}: {e}")
            return ""

    async def process_message(self, http_session, user_id: str, message: str) -> Dict[str, Any]:
        try:
            session = self.session_manager.get_session(user_id)
            intent_params = await self.intent_classifier.classify_with_llm(
                http_session,
                message,
                {'last_movies': session.last_movies, 'last_params': session.last_params}
            )
            intent = intent_params.get("intent", "initial")
            logger.info(f"Обработка запроса: intent={intent}, user_id={user_id}")

            if intent == "info":
                result = await self._handle_info_request(http_session, message, intent_params, session)
            elif intent == "similar":
                result = await self._handle_similar_request(http_session, message, intent_params, session)
            elif intent == "alternative":
                result = await self._handle_refine_request(http_session, message, intent_params, session)
            else:
                result = await self._handle_general_request(http_session, message, intent_params, session)

            self._update_session(session, result)
            return result
        except Exception as e:
            logger.error(f"Ошибка обработки сообщения: {e}", exc_info=True)
            return {
                "response": "Извините, произошла ошибка при обработке вашего запроса. Попробуйте еще раз.",
                "needs_clarification": True
            }

    async def _handle_info_request(self, http_session, message: str, params: Dict, session: UserSession) -> Dict[
        str, Any]:
        target_movie = params.get("target_movie")
        if not target_movie:
            return {
                "response": "О каком фильме вы хотите узнать? Напишите название фильма.",
                "needs_clarification": True
            }
        found_movies = await self.movie_agent.search_by_title(http_session, target_movie)
        if not found_movies:
            return {
                "response": f"Не удалось найти информацию о фильме «{target_movie}». Проверьте название и попробуйте еще раз.",
                "needs_clarification": True
            }
        movie = found_movies[0]
        response_text = self._generate_single_movie_response(movie)
        return {
            "response": response_text,
            "movie": movie,
            "movies_list": [movie],
            "parameters": params,
            "needs_clarification": False
        }

    async def _handle_similar_request(self, http_session, message: str, params: Dict, session: UserSession) -> Dict[
        str, Any]:
        if not session.last_movies:
            return {
                "response": "У меня нет информации о предыдущих рекомендациях. Сначала найдите фильм, а потом попросите похожие.",
                "needs_clarification": True
            }
        self.movie_agent.clear_cache()
        last_movies = session.last_movies

        # Определяем movie_type
        if last_movies and isinstance(last_movies[0], dict):
            detected_type = last_movies[0].get('type') or last_movies[0].get('movie_type')
            movie_type = 'tv-series' if detected_type == 'tv-series' else 'movie'
        else:
            movie_type = session.last_params.get('movie_type', 'movie')

        # 🔑 1. Извлекаем страну и год/диапазон из НОВОГО запроса (params), иначе — из сессии
        country = params.get('country') or session.last_params.get('country')
        explicit_year = params.get('year')
        explicit_year_range = params.get('year_range')

        # 🔑 2. Жанр берём из последнего фильма (или доминирующий), если не указан явно
        genre = None
        if len(last_movies) == 1:
            base = last_movies[0]
            genre = base.get('genre', '').split(',')[0].strip() if base.get('genre') else None
        else:
            genres = []
            for m in last_movies:
                if m.get('genre'):
                    genres.extend([g.strip() for g in m['genre'].split(',') if g.strip()])
            if genres:
                from collections import Counter
                genre = Counter(genres).most_common(1)[0][0]

        # 🔑 3. Определяем диапазон лет
        year_range = None
        min_rating = 6.5

        if explicit_year_range:
            # Явный диапазон: "2000-2010"
            year_range = explicit_year_range
        elif explicit_year is not None:
            # Десятилетие: "2000" → (2000, 2009)
            if explicit_year % 10 == 0 and 1900 <= explicit_year <= 2020:
                year_range = (explicit_year, min(explicit_year + 9, 2025))
            else:
                # Конкретный год: "2005" → (2002, 2008)
                year_range = (max(1900, explicit_year - 3), min(2025, explicit_year + 3))
        else:
            # Нет явного года — берём из последнего фильма
            if len(last_movies) == 1:
                year = last_movies[0].get('year')
                rating = last_movies[0].get('rating_imdb') or last_movies[0].get('rating_kp') or 6.5
                min_rating = max(6.0, rating - 0.5)
                if year:
                    year_range = (max(1900, year - 3), min(2025, year + 3))
            else:
                years = [m.get('year') for m in last_movies if m.get('year')]
                if years:
                    year_range = (max(1900, min(years) - 2), min(2025, max(years) + 2))
                ratings = [m.get('rating_imdb') or m.get('rating_kp') for m in last_movies]
                ratings = [r for r in ratings if r is not None]
                if ratings:
                    min_rating = max(6.0, sorted(ratings)[len(ratings) // 2] - 0.5)

        # 🔑 4. Выполняем поиск с учётом всех параметров
        movies = await self.movie_agent.recommend_movies(
            http_session,
            genre_name=genre,
            year_range=year_range,
            min_imdb_rating=min_rating,
            limit=25,
            movie_type=movie_type,
            country=country  # ← страна тоже из нового запроса или сессии
        )

        seen_ids = {m.get('id') for m in last_movies if m.get('id')}
        new_movies = [m for m in movies if m.get('id') not in seen_ids]
        final_movies = (new_movies[:13] or movies[:13])

        response_text, reply_markup = self._generate_list_response(
            final_movies,
            "Вот что ещё может вам понравиться:"
        )
        return {
            "response": response_text,
            "reply_markup": reply_markup,
            "movies_list": final_movies,
            "parameters": {"movie_type": movie_type, "country": country},
            "needs_clarification": False
        }

    async def _handle_refine_request(self, http_session, message: str, params: Dict, session: UserSession) -> Dict[
        str, Any]:
        if not session.last_params:
            return {
                "response": "Сначала задайте критерии поиска, например: «комедии 2020-х»",
                "needs_clarification": True
            }

        last_params = session.last_params.copy()
        last_movie_ids = {m['id'] for m in session.last_movies if m.get('id')}
        movie_type = last_params.get('movie_type', 'movie')
        mood_genres = last_params.get('mood_genres') or [last_params.get('genre')] if last_params.get('genre') else [
            'комедия']

        all_new_movies = []
        seen_ids = set(last_movie_ids)
        for genre in mood_genres:
            if len(all_new_movies) >= 13:
                break
            raw_movies = await self.movie_agent.recommend_movies(
                http_session,
                genre_name=genre,
                year=last_params.get('year'),
                year_range=last_params.get('year_range'),
                actor=last_params.get('actor'),
                director=last_params.get('director'),
                country=last_params.get('country'),
                min_imdb_rating=last_params.get('min_rating') or 6.0,
                limit=30,
                movie_type=movie_type
            )
            for m in raw_movies:
                mid = m.get('id')
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    all_new_movies.append(m)
                    if len(all_new_movies) >= 13:
                        break

        if not all_new_movies:
            raw_movies = await self.movie_agent.recommend_movies(
                http_session,
                genre_name=last_params.get('genre'),
                year=last_params.get('year'),
                year_range=last_params.get('year_range'),
                actor=last_params.get('actor'),
                director=last_params.get('director'),
                country=last_params.get('country'),
                min_imdb_rating=last_params.get('min_rating') or 6.0,
                limit=13,
                movie_type=movie_type
            )
            all_new_movies = raw_movies[:13]
            response_text, reply_markup = self._generate_list_response(
                all_new_movies,
                "Повторяю предыдущие рекомендации:"
            )
        else:
            response_text, reply_markup = self._generate_list_response(
                all_new_movies,
                "Вот другие варианты:"
            )

        return {
            "response": response_text,
            "reply_markup": reply_markup,
            "movies_list": all_new_movies,
            "parameters": {**last_params, "movie_type": movie_type},
            "needs_clarification": False
        }

    async def _handle_general_request(self, http_session, message: str, params: Dict, session: UserSession) -> Dict[
        str, Any]:
        reply_markup = None
        message_lower = message.lower()
        logger.info(f"Обработка общего запроса: '{message}', params: {params}")
        # === Распознавание настроения ===
        mood_genres = []
        mood_triggers = {
            'грустн': ['грустн', 'плохое настроение', 'поднять настроение', 'грущу', 'грусть', 'хочу радости',
                       'подавлен', 'депресс', 'тоска', 'печал', 'уныл'],
            'весел': ['весел', 'смех', 'смешн', 'посмеяться', 'радост', 'хорошее настроение', 'радость',
                      'настроение отличное', 'счастлив', 'улыбк', 'забавн', 'юмор'],
            'устал': ['устал', 'выгор', 'энергии нет', 'отдохнуть', 'расслабиться', 'спокойн', 'тихий вечер',
                      'ничего напряжённого', 'без экшена', 'лёгкий фильм'],
            'скучно': ['скучно', 'нечего смотреть', 'занять себя', 'развлечься', 'что-то интересное', 'надоело всё',
                       'ищу что-то новое'],
            'страшн': ['страшн', 'испуг', 'боюсь', 'ужас', 'мистик', 'триллер', 'пуга', 'жутк', 'напряг', 'напряжённый',
                       'напрячь нервы', 'щекотка для нервов'],
            'романт': ['романт', 'влюблен', 'любовь', 'пара', 'вдвоем', 'нежн', 'сердечко', 'романтический вечер',
                       'чувств', 'влюблённость'],
            'адреналин': ['адреналин', 'экшн', 'боевик', 'напряжение', 'динамик', 'крутой', 'взрывы', 'гонки', 'погони',
                          'герои', 'спасение мира'],
            'умный': ['умный', 'глубок', 'философ', 'мысл', 'интеллектуальн', 'осмысл', 'не для всех', 'сложный',
                      'мозг', 'рефлексия', 'медитативн']
        }
        for mood_key, phrases in mood_triggers.items():
            if any(phrase in message_lower for phrase in phrases):
                mood_genres = self.mood_to_genre.get(mood_key, ['комедия'])
                logger.info(f"Определены жанры по настроению '{mood_key}': {mood_genres}")
                break

        explicit_genre = params.get('genre')
        if explicit_genre:
            mood_genres = [explicit_genre]

        use_query = params.get('actor') or None
        if not mood_genres and not explicit_genre and len(message.split()) <= 5:
            use_query = message

        year = params.get('year')
        year_range = params.get('year_range')
        is_decade = (year is not None and year % 10 == 0 and 1900 <= year <= 2020)
        current_year = 2025
        if is_decade and year_range is None:
            year_range = (year, min(year + 9, current_year))

        min_rating = params.get('min_rating') or (6.5 if is_decade else 6.0)
        limit = params.get('count') or 13  # ← Изменено: убрана привязка к is_decade
        movie_type = params.get('movie_type', 'movie')
        content_type = "сериалы" if movie_type == 'tv-series' else "фильмы"
        # === Множественный поиск по жанрам ===
        all_movies = []
        seen_ids = set()
        if mood_genres:
            for genre in mood_genres:
                if len(all_movies) >= limit:
                    break
                movies = await self.movie_agent.recommend_movies(
                    http_session,
                    genre_name=genre,
                    year=year if not is_decade else None,
                    year_range=year_range,
                    actor=params.get('actor'),
                    director=params.get('director'),
                    country=params.get('country'),
                    min_imdb_rating=min_rating,
                    limit=limit,
                    movie_type=movie_type,
                    query=use_query
                )
                for m in movies:
                    mid = m.get('id')
                    if mid and mid not in seen_ids:
                        seen_ids.add(mid)
                        all_movies.append(m)
                        if len(all_movies) >= limit:
                            break
        else:
            movies = await self.movie_agent.recommend_movies(
                http_session,
                genre_name=None,
                year=year if not is_decade else None,
                year_range=year_range,
                actor=params.get('actor'),
                director=params.get('director'),
                country=params.get('country'),
                min_imdb_rating=min_rating,
                limit=limit,
                movie_type=movie_type,
                query=use_query
            )
            for m in movies:
                mid = m.get('id')
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    all_movies.append(m)
                    if len(all_movies) >= limit:
                        break

        movies = all_movies[:limit]
        logger.info(f"Найдено {movie_type}: {len(movies)}")

        if not movies:
            content_type = "сериалы" if movie_type == 'tv-series' else "фильмы"
            error_parts = []
            if params.get('country'):
                error_parts.append(f"стране '{params['country']}'")
            if params.get('director'):
                error_parts.append(f"режиссёру '{params['director']}'")
            if year_range:
                error_parts.append(f"{year_range[0]}-{year_range[1]} годах")
            elif year:
                error_parts.append(f"{year} году")
            if mood_genres or explicit_genre:
                genre_display = mood_genres[0] if mood_genres else explicit_genre
                error_parts.append(f"жанре '{genre_display}'")
            error_message = f"К сожалению, не удалось найти подходящие {content_type}."
            if error_parts:
                error_message = f"К сожалению, не удалось найти качественные {content_type} по вашему запросу ({', '.join(error_parts)})."
            return {
                "response": error_message,
                "reply_markup": None,
                "needs_clarification": True,
                "parameters": {**params, "movie_type": movie_type}
            }

        if mood_genres:
            mood_text = self._get_mood_text(message_lower)
            response_text, reply_markup = self._generate_list_response(
                movies,
                f"Вот {content_type}, которые помогут {mood_text}:",
                limit=limit
            )
        elif any(word in message_lower for word in ['топ', 'лучш', 'рейтинг']):
            header = self._generate_top_header(
                mood_genres[0] if mood_genres else None,
                year,
                params.get('country'),
                year_range,
                content_type
            )
            response_text, reply_markup = self._generate_list_response(movies, header, limit=limit)
        else:
            header = self._generate_search_header(
                mood_genres[0] if mood_genres else None,
                year,
                params.get('country'),
                year_range,
                content_type
            )
            response_text, reply_markup = self._generate_list_response(movies, header, limit=limit)

        return {
            "response": response_text,
            "reply_markup": reply_markup,
            "movies_list": movies,
            "parameters": {
                **params,
                "genre": mood_genres[0] if mood_genres else None,
                "mood_genres": mood_genres,
                "mood": bool(mood_genres),
                "movie_type": movie_type
            },
            "needs_clarification": False
        }

    # === Синхронные вспомогательные методы (не делают I/O) ===

    def _get_mood_text(self, message_lower: str) -> str:
        if any(w in message_lower for w in ['грустн', 'печал', 'тоска', 'уныл']):
            return "поднять настроение"
        elif any(w in message_lower for w in ['весел', 'смех', 'смешн', 'радост', 'юмор']):
            return "соответствовать вашему весёлому настроению"
        elif any(w in message_lower for w in ['устал', 'выгор', 'расслаб', 'спокойн']):
            return "помочь расслабиться и отдохнуть"
        elif any(w in message_lower for w in ['скучно', 'нечего смотреть', 'занять']):
            return "развлечь и удивить"
        elif any(w in message_lower for w in ['страшн', 'испуг', 'пуга', 'жутк', 'напряг']):
            return "пощекотать нервы"
        elif any(w in message_lower for w in ['романт', 'влюблен', 'любовь', 'нежн']):
            return "создать романтическое настроение"
        elif any(w in message_lower for w in ['адреналин', 'экшн', 'боевик', 'взрывы']):
            return "зарядить адреналином"
        elif any(w in message_lower for w in ['умный', 'глубок', 'философ', 'интеллектуальн']):
            return "заставить задуматься"
        else:
            return "подойти вашему настроению"

    def _generate_top_header(self, genre: Optional[str], year: Optional[int], country: Optional[str] = None,
                             year_range: Optional[tuple] = None, content_type: str = "фильмов") -> str:
        parts = []
        if country:
            parts.append(f"из {country}")
        if genre:
            parts.append(f"в жанре {genre}")
        if year_range:
            if year_range[0] // 10 == year_range[1] // 10 and year_range[1] - year_range[0] == 9:
                decade = year_range[0]
                parts.append(f"{decade}-х годов")
            else:
                parts.append(f"{year_range[0]}–{year_range[1]} годов")
        elif year:
            if year % 10 == 0:
                parts.append(f"{year}-х годов")
            else:
                parts.append(f"в {year} году")  # ← добавлено "в"
        header = f"Топ {content_type}"
        if parts:
            header += " " + " ".join(parts)
        return header + ":"

    def _generate_search_header(self, genre: Optional[str], year: Optional[int], country: Optional[str] = None,
                                year_range: Optional[tuple] = None, content_type: str = "фильмов") -> str:
        parts = []
        if country:
            parts.append(f"из {country}")
        if genre:
            parts.append(f"в жанре {genre}")
        if year_range:
            if year_range[0] // 10 == year_range[1] // 10 and year_range[1] - year_range[0] == 9:
                decade = year_range[0]
                parts.append(f"{decade}-х годов")
            else:
                parts.append(f"{year_range[0]}–{year_range[1]} годов")
        elif year:
            if year % 10 == 0:
                parts.append(f"{year}-х годов")
            else:
                parts.append(f"в {year} году")  # ← "в"
        header = f"Рекомендации {content_type}"
        if parts:
            header += " " + " ".join(parts)
        return header + ":"

    def _generate_list_response(self, movies: List[Dict], header: str, limit: int = 8) -> tuple[
        str, InlineKeyboardMarkup]:
        response = f"<strong>{header}</strong>\n"
        buttons = []
        for i, movie in enumerate(movies[:limit], 1):
            title = movie.get('title', '—')
            year = movie.get('year', '')
            rating = movie.get('rating', '—')
            response += f"{i}. <strong>{title}</strong> ({year}) — ⭐ {rating}\n"
            movie_id = movie.get('id') or 0
            callback_data = f"info:{movie_id}"
            buttons.append([InlineKeyboardButton(f"Подробнее: {title}", callback_data=callback_data)])
        keyboard = InlineKeyboardMarkup(buttons)
        return response, keyboard

    def _generate_single_movie_response(self, movie: Dict) -> str:
        title = movie.get('title', '—')
        year = movie.get('year', '')
        genre = movie.get('genre', '—')
        rating = movie.get('rating', '—')
        description = movie.get('description', '')
        return f"🎬 <strong>{title}</strong> ({year}) — {genre} с рейтингом {rating}.\n{description}"

    def _update_session(self, session: UserSession, result: Dict):
        if "movies_list" in result:
            session.last_movies = result["movies_list"]
        if "parameters" in result:
            session.last_params = result["parameters"]
        session.update_activity()

    def clear_user_session(self, user_id: str):
        self.session_manager.clear_session(user_id)
