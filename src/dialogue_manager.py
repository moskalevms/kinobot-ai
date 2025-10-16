# src/dialogue_manager.py
import os
import logging
import re
from typing import Dict, Any, List, Optional, Tuple
from .movie_agent import MovieAgent
from .session_manager import SessionManager, UserSession
from .intent_classifier import IntentClassifier
from .llm_router import LLMRouter
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import random

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
        # Убрали биографию, документальный, артхаус из рекомендаций по умолчанию
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

    def process_message(self, user_id: str, message: str) -> Dict[str, Any]:
        try:
            session = self.session_manager.get_session(user_id)
            intent_params = self.intent_classifier.classify_with_llm(
                message,
                {'last_movies': session.last_movies, 'last_params': session.last_params}
            )
            intent = intent_params.get("intent", "initial")
            logger.info(f"Обработка запроса: intent={intent}, user_id={user_id}")
            if intent == "info":
                result = self._handle_info_request(message, intent_params, session)
            elif intent == "similar":
                result = self._handle_similar_request(message, intent_params, session)
            elif intent == "alternative":
                result = self._handle_refine_request(message, intent_params, session)
            else:
                result = self._handle_general_request(message, intent_params, session)
            self._update_session(session, result)
            return result
        except Exception as e:
            logger.error(f"Ошибка обработки сообщения: {e}", exc_info=True)
            return {
                "response": "Извините, произошла ошибка при обработке вашего запроса. Попробуйте еще раз.",
                "needs_clarification": True
            }

    def _handle_info_request(self, message: str, params: Dict, session: UserSession) -> Dict[str, Any]:
        target_movie = params.get("target_movie")
        if not target_movie:
            return {
                "response": "О каком фильме вы хотите узнать? Напишите название фильма.",
                "needs_clarification": True
            }
        found_movies = self.movie_agent.search_by_title(target_movie)
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

    def _handle_similar_request(self, message: str, params: Dict, session: UserSession) -> Dict[str, Any]:
        if not session.last_movies:
            return {
                "response": "У меня нет информации о предыдущих рекомендациях. Сначала найдите фильм, а потом попросите похожие.",
                "needs_clarification": True
            }

        self.movie_agent.clear_cache()
        last_movies = session.last_movies
        if session.last_movies and isinstance(session.last_movies[0], dict):
            detected_type = session.last_movies[0].get('type') or session.last_movies[0].get('movie_type')
            if detected_type == 'tv-series':
                movie_type = 'tv-series'
            elif detected_type == 'movie':
                movie_type = 'movie'
            else:
                movie_type = session.last_params.get('movie_type', 'movie')
        else:
            movie_type = session.last_params.get('movie_type', 'movie')

        # === Сценарий 1: один фильм ===
        if len(last_movies) == 1:
            base = last_movies[0]
            genre = base.get('genre', '').split(',')[0].strip() if base.get('genre') else None
            year = base.get('year')
            rating = base.get('rating_imdb') or base.get('rating_kp') or 6.5
            min_rating = max(6.0, rating - 0.5)
            year_range = (year - 3, year + 3) if year else None

            movies = self.movie_agent.recommend_movies(
                genre_name=genre,
                year_range=year_range,
                min_imdb_rating=min_rating,
                limit=20,
                movie_type=movie_type  # ← добавлено
            )

        # === Сценарий 2: подборка (≥2 фильмов) ===
        else:
            genres = []
            ratings = []
            years = []
            for m in last_movies:
                if m.get('genre'):
                    genres.extend([g.strip() for g in m['genre'].split(',') if g.strip()])
                r = m.get('rating_imdb') or m.get('rating_kp')
                if r:
                    ratings.append(r)
                y = m.get('year')
                if y:
                    years.append(y)

            from collections import Counter
            dominant_genre = Counter(genres).most_common(1)[0][0] if genres else None
            min_rating = sorted(ratings)[len(ratings) // 2] if ratings else 6.5
            year_range = (min(years) - 2, max(years) + 2) if years else None

            movies = self.movie_agent.recommend_movies(
                genre_name=dominant_genre,
                year_range=year_range,
                min_imdb_rating=min_rating,
                limit=25,
                movie_type=movie_type
            )

        # Исключаем дубликаты
        seen_ids = {m.get('id') for m in last_movies if m.get('id')}
        new_movies = [m for m in movies if m.get('id') not in seen_ids]
        final_movies = (new_movies[:13] or movies[:13])

        base_title = last_movies[0].get('title', 'фильм')
        content_type = "сериалов" if movie_type == 'tv-series' else "фильмов"  # ← для заголовка
        response_text, reply_markup = self._generate_list_response(
            final_movies,
            "Вот что ещё может вам понравиться:"
        )

        return {
            "response": response_text,
            "reply_markup": reply_markup,
            "movies_list": final_movies,
            "parameters": {"similar_to": base_title, "movie_type": movie_type},  # ← добавлено
            "needs_clarification": False
        }

    def _handle_refine_request(self, message: str, params: Dict, session: UserSession) -> Dict[str, Any]:
        if not session.last_params:
            return {
                "response": "Сначала задайте критерии поиска, например: «комедии 2020-х»",
                "needs_clarification": True
            }
        last_params = session.last_params.copy()
        last_movie_ids = {m['id'] for m in session.last_movies if m.get('id')}
        movie_type = last_params.get('movie_type', 'movie')
        mood_genres = last_params.get('mood_genres') or [last_params.get('genre')] if last_params.get('genre') else ['комедия']

        all_new_movies = []
        seen_ids = set(last_movie_ids)

        for genre in mood_genres:
            if len(all_new_movies) >= 13:
                break
            raw_movies = self.movie_agent.recommend_movies(
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
            # fallback: возвращаем исходный список
            raw_movies = self.movie_agent.recommend_movies(
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
            content_type = "сериалов" if movie_type == 'tv-series' else "фильмов"
            response_text, reply_markup = self._generate_list_response(
                all_new_movies,
                f"Повторяю предыдущие рекомендации {content_type}:".replace("фильмов", content_type)
            )
        else:
            content_type = "сериалов" if movie_type == 'tv-series' else "фильмов"
            response_text, reply_markup = self._generate_list_response(
                all_new_movies,
                f"Вот другие варианты {content_type}:".replace("фильмов", content_type)
            )

        return {
            "response": response_text,
            "reply_markup": reply_markup,
            "movies_list": all_new_movies,
            "parameters": {**last_params, "movie_type": movie_type},  # ← добавлено
            "needs_clarification": False
        }

    def _handle_general_request(self, message: str, params: Dict, session: UserSession) -> Dict[str, Any]:
        reply_markup = None
        message_lower = message.lower()
        logger.info(f"Обработка общего запроса: '{message}', params: {params}")
        # === Распознавание настроения ===
        mood_genres = []
        mood_triggers = {
            'грустн': ['грустн', 'плохое настроение', 'поднять настроение', 'грущу', 'грусть', 'хочу радости', 'подавлен', 'депресс', 'тоска', 'печал', 'уныл'],
            'весел': ['весел', 'смех', 'смешн', 'посмеяться', 'радост', 'хорошее настроение', 'радость', 'настроение отличное', 'счастлив', 'улыбк', 'забавн', 'юмор'],
            'устал': ['устал', 'выгор', 'энергии нет', 'отдохнуть', 'расслабиться', 'спокойн', 'тихий вечер', 'ничего напряжённого', 'без экшена', 'лёгкий фильм'],
            'скучно': ['скучно', 'нечего смотреть', 'занять себя', 'развлечься', 'что-то интересное', 'надоело всё', 'ищу что-то новое'],
            'страшн': ['страшн', 'испуг', 'боюсь', 'ужас', 'мистик', 'триллер', 'пуга', 'жутк', 'напряг', 'напряжённый', 'напрячь нервы', 'щекотка для нервов'],
            'романт': ['романт', 'влюблен', 'любовь', 'пара', 'вдвоем', 'нежн', 'сердечко', 'романтический вечер', 'чувств', 'влюблённость'],
            'адреналин': ['адреналин', 'экшн', 'боевик', 'напряжение', 'динамик', 'крутой', 'взрывы', 'гонки', 'погони', 'герои', 'спасение мира'],
            'умный': ['умный', 'глубок', 'философ', 'мысл', 'интеллектуальн', 'осмысл', 'не для всех', 'сложный', 'мозг', 'рефлексия', 'медитативн']
        }
        for mood_key, phrases in mood_triggers.items():
            if any(phrase in message_lower for phrase in phrases):
                mood_genres = self.mood_to_genre.get(mood_key, ['комедия'])
                logger.info(f"Определены жанры по настроению '{mood_key}': {mood_genres}")
                break
        explicit_genre = params.get('genre')
        if explicit_genre:
            mood_genres = [explicit_genre]
        # Определение query
        use_query = None
        actor = params.get('actor')
        if actor:
            # Используем имя актёра как query, если он указан
            use_query = actor
        elif not mood_genres and not explicit_genre:
            if not params.get('country') and not params.get('year') and not params.get('director') and len(
                    message.split()) <= 5:
                use_query = message
        # Обработка десятилетий
        year = params.get('year')
        year_range = None
        is_decade = False
        current_year = 2025
        decade_match = re.search(r'(\d{3})0-х', message_lower)
        if decade_match:
            decade = int(decade_match.group(1) + "0")
            year_range = (decade, min(decade + 9, current_year))
            is_decade = True
            logger.info(f"Обнаружено десятилетие в запросе: {decade}-е → диапазон {year_range}")
        elif year and 1900 <= year <= 2020 and year % 10 == 0:
            is_decade = True
            year_range = (year, min(year + 9, current_year))
            logger.info(f"Обнаружено десятилетие по году: {year}-е → диапазон {year_range}")
        min_rating = params.get('min_rating') or (6.5 if is_decade else 6.0)
        limit = params.get('count') or 13  # ← Изменено: убрана привязка к is_decade
        movie_type = params.get('movie_type', 'movie')
        content_type = "сериалов" if movie_type == 'tv-series' else "фильмов"
        # === Множественный поиск по жанрам ===
        all_movies = []
        seen_ids = set()
        if mood_genres:
            for genre in mood_genres:
                if len(all_movies) >= limit:
                    break
                movies = self.movie_agent.recommend_movies(
                    genre_name=genre,
                    year=year if not is_decade else None,
                    year_range=year_range,
                    actor=params.get('actor'),
                    director=params.get('director'),
                    country=params.get('country'),
                    min_imdb_rating=min_rating,
                    limit=limit,
                    movie_type=movie_type,  # ← добавлено
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
            # fallback: если нет жанра/настроения, ищем без жанра (общий топ)
            logger.info(f"Нет жанра или настроения — fallback-поиск без жанра с min_rating={min_rating}")
            movies = self.movie_agent.recommend_movies(
                genre_name=None,  # Без жанра
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
            if error_parts:
                error_message = f"К сожалению, не удалось найти качественные {content_type} по вашему запросу ({', '.join(error_parts)})."
            else:
                error_message = f"К сожалению, не удалось найти подходящие {content_type}."
            suggestions = []
            if year_range and year_range[0] < 1980:
                suggestions.append("попробуйте поискать {content_type} более позднего периода")
            if params.get('country') and params.get('country') not in ['США', 'Россия']:
                suggestions.append("попробуйте изменить страну поиска")
            if (mood_genres or explicit_genre) and (mood_genres[0] if mood_genres else explicit_genre) in ['ужасы', 'мистика']:
                suggestions.append("попробуйте более популярные жанры")
            if suggestions:
                error_message += f"\n💡 Совет: {', '.join(suggestions)}."
            return {
                "response": error_message,
                "reply_markup": None,  # ← ДОБАВЛЕНО
                "needs_clarification": True,
                "parameters": {**params, "movie_type": movie_type}  # ← добавлено
            }
        # Генерация ответа
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
            "parameters": {**params, "genre": mood_genres[0] if mood_genres else None, "mood_genres": mood_genres, "mood": bool(mood_genres), "movie_type": movie_type},  # ← добавлено movie_type
            "needs_clarification": False
        }

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
                             year_range: Optional[tuple] = None, content_type: str = "фильмов") -> str:  # ← добавлен content_type
        parts = []
        if country:
            parts.append(f"из {country}")
        if genre:
            parts.append(f"в жанре {genre}")
        if year_range:
            parts.append(f"{year_range[0]}-{year_range[1]} годов")
        elif year:
            if year % 10 == 0:
                parts.append(f"{year}-х годов")
            else:
                parts.append(f"{year} году")
        header = f"Топ {content_type}"  # ← динамично
        if parts:
            header += " " + " ".join(parts)
        return header + ":"

    def _generate_search_header(self, genre: Optional[str], year: Optional[int], country: Optional[str] = None,
                                year_range: Optional[tuple] = None, content_type: str = "фильмов") -> str:  # ← добавлен content_type
        parts = []
        if country:
            parts.append(f"из {country}")
        if genre:
            parts.append(f"в жанре {genre}")
        if year_range:
            parts.append(f"{year_range[0]}-{year_range[1]} годов")
        elif year:
            if year % 10 == 0:
                parts.append(f"{year}-х годов")
            else:
                parts.append(f"{year} году")
        header = f"Рекомендации {content_type}"  # ← динамично
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
            # callback_data ограничено 64 байтами → используем только ID
            callback_data = f"info:{movie_id}"
            buttons.append([InlineKeyboardButton(f"Подробнее: {title}", callback_data=callback_data)])
        keyboard = InlineKeyboardMarkup(buttons)
        return response, keyboard

    def _generate_mood_response(self, movies: List[Dict], mood_text: str) -> str:
        response = f"Вот фильмы, которые помогут {mood_text}:\n\n"
        for i, movie in enumerate(movies, 1):
            title = movie.get('title', '—')
            year = movie.get('year', '')
            rating = movie.get('rating', '—')
            response += f"{i}. <strong>{title}</strong> ({year}) — ⭐ {rating}\n"
        return response

    def _generate_single_movie_response(self, movie: Dict) -> str:
        title = movie.get('title', '—')
        year = movie.get('year', '')
        genre = movie.get('genre', '—')
        rating = movie.get('rating', '—')
        description = movie.get('description', '')
        return f"🎬 <strong>{title}</strong> ({year}) — {genre} с рейтингом {rating}.\n\n{description}"

    def _update_session(self, session: UserSession, result: Dict):
        if "movies_list" in result:
            session.last_movies = result["movies_list"]
        if "parameters" in result:
            session.last_params = result["parameters"]
        session.update_activity()

    def clear_user_session(self, user_id: str):
        self.session_manager.clear_session(user_id)