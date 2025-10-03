import re
import os
import json
import random
from typing import Dict, Any, List, Optional
from html import escape
from flask import session
from .llm_router import LLMRouter
from src.movie_agent import MovieAgent


class DialogMovieAgent:
    def __init__(self):
        self.llm_router = LLMRouter()
        self.movie_agent = MovieAgent(use_api=True)
        self.prompts_dir = os.path.join(os.path.dirname(__file__), '..', 'prompts')

    def _load_prompt(self, filename: str) -> str:
        path = os.path.join(self.prompts_dir, filename)
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()

    def _extract_parameters(self, user_message: str) -> Dict[str, Any]:
        system_prompt = self._load_prompt('parameter_extraction_prompt.txt')
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ]
        response = self.llm_router.call_llm(messages, max_tokens=250)
        if not response:
            return self._empty_params()
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        json_str = json_match.group(0) if json_match else response.strip()
        try:
            params = json.loads(json_str)
            for key in ["intent", "target_movie", "genre", "actor", "director", "studio", "country", "mood"]:
                params.setdefault(key, None)
            params.setdefault("count", None)
            params.setdefault("min_rating", None)
            params.setdefault("year", None)
            # Обработка типов
            if "count" in params:
                try:
                    params["count"] = int(params["count"])
                except (TypeError, ValueError):
                    params["count"] = None
            if "year" in params:
                try:
                    params["year"] = int(params["year"])
                except (TypeError, ValueError):
                    params["year"] = None
            if "min_rating" in params:
                try:
                    params["min_rating"] = float(params["min_rating"])
                except (TypeError, ValueError):
                    params["min_rating"] = None
            if "intent" not in params:
                params["intent"] = "initial"
            return params
        except (json.JSONDecodeError, TypeError):
            return self._empty_params()

    def _empty_params(self):
        return {
            "intent": "initial",
            "target_movie": None,
            "genre": None,
            "year": None,
            "actor": None,
            "director": None,
            "studio": None,
            "country": None,
            "mood": None,
            "count": None,
            "min_rating": None
        }

    def _is_tv_series_request(self, user_message: str) -> bool:
        return any(w in user_message.lower() for w in ["сериал", "сезон", "эпизод"])

    def _generate_single(self, movie: Dict[str, Any]) -> str:
        prompt_template = self._load_prompt('response_generation_prompt.txt')
        prompt = prompt_template.format(
            title=movie.get('title', '—'),
            year=movie.get('year', '—'),
            genre=movie.get('genre', '—'),
            rating=movie.get('rating', '—'),
            description=movie.get('description', 'Описание отсутствует.')
        )
        messages = [{"role": "user", "content": prompt}]
        response = self.llm_router.call_llm(messages, max_tokens=300)
        if response:
            return response.strip()
        title = escape(movie.get('title', '—'))
        year = escape(str(movie.get('year', '—')))
        rating = escape(str(movie.get('rating', '—')))
        return f'🎬 <strong>{title}</strong> ({year}) — ⭐ {rating}'

    def _generate_list(self, movies: List[Dict[str, Any]], clickable: bool = False) -> str:
        if not movies:
            return "<p>Ничего не найдено 😔</p>"
        items = []
        for i, m in enumerate(movies[:10], 1):
            title = escape(m.get('title', ''))
            year = escape(str(m.get('year', '')))
            rating = escape(str(m.get('rating', '')))
            movie_id = m.get('id') or f"title_{i}"
            if clickable:
                item = (
                    f'<div class="movie-item" data-movie-id="{movie_id}" '
                    f'data-movie-title="{escape(title)}" style="cursor: pointer; text-decoration: underline;">'
                    f'{i}. <strong>{title}</strong> ({year}) — ⭐ {rating}'
                    f'</div>'
                )
            else:
                item = f'{i}. <strong>{title}</strong> ({year}) — ⭐ {rating}'
                item = f'<div class="movie-item">{item}</div>'
            items.append(item)
        items_html = "\n".join(items)
        return f'<div class="movie-list">🍿 Подборка:<br>{items_html}</div>'

    def chat(self, user_message: str, history: Optional[List[Dict[str, str]]] = None) -> dict:
        params = self._extract_parameters(user_message)

        # Автоустановка min_rating = 6.0 для "лучших", "топ" и т.п.
        user_message_lower = user_message.lower()
        if params.get("min_rating") is None:
            if any(word in user_message_lower for word in [
                "лучший", "лучшие", "лучших", "рейтинговых", "топ", "top", "best"
            ]):
                params["min_rating"] = 6.0

        intent = params.get("intent")
        target_movie_title = params.get("target_movie")

        # 1. Запрос информации о конкретном фильме
        if intent == "info" and target_movie_title:
            found = self.movie_agent.search_by_title(target_movie_title)
            movie = found[0] if found else None
            if movie:
                response_text = self._generate_single(movie)
                return {
                    "response": response_text,
                    "needs_clarification": False,
                    "parameters": params,
                    "movie": movie,
                    "movies_list": None
                }
            else:
                return {
                    "response": f"Не нашёл фильм «{target_movie_title}».",
                    "needs_clarification": True,
                    "parameters": params
                }

        # 2. Похожие фильмы
        if intent == "similar":
            last_movies = session.get('last_movies', [])
            target_movie = None
            if target_movie_title:
                for m in last_movies:
                    if target_movie_title.lower() in m.get('title', '').lower():
                        target_movie = m
                        break
                if not target_movie:
                    found = self.movie_agent.search_by_title(target_movie_title)
                    target_movie = found[0] if found else None
            elif last_movies:
                target_movie = last_movies[0]

            if target_movie:
                genres = target_movie.get('genre', '')
                genre_list = [g.strip() for g in genres.split(',') if g.strip()]
                primary_genre = genre_list[0] if genre_list else None
                rating = target_movie.get('rating_imdb') or target_movie.get('rating_kp')
                min_rating = max(0.0, float(rating) - 1.0) if rating else None
                country = target_movie.get('country', 'США')

                movies = self.movie_agent.recommend_movies(
                    genre_name=primary_genre,
                    min_imdb_rating=min_rating,
                    country=country,
                    limit=5,
                    movie_type='movie'
                )
                if movies and not (isinstance(movies, dict) and "error" in movies):
                    response_text = self._generate_list(movies, clickable=True)
                    return {
                        "response": response_text,
                        "needs_clarification": False,
                        "parameters": params,
                        "movies_list": movies
                    }

        # 3. Обычный поиск
        genre = params.get("genre")
        year = params.get("year")
        actor = params.get("actor")
        director = params.get("director")
        studio = params.get("studio")
        country = params.get("country")
        mood = params.get("mood")
        count = params.get("count") or 10  # Теперь по умолчанию 10
        min_rating = params.get("min_rating")

        MOOD_TO_GENRE = {
            "лёгкий": ["комедия", "мелодрама", "мультфильм", "семейный"],
            "серьёзный": ["драма", "биография", "военный", "исторический"],
            "адреналин": ["боевик", "триллер", "приключения", "фантастика"],
            "для поднятия настроения": ["комедия", "мюзикл", "романтика"],
            "страшный": ["ужасы", "триллер", "мистика"],
            "умный": ["драма", "биография", "детектив", "фантастика"]
        }

        if mood and not genre:
            genre = random.choice(MOOD_TO_GENRE.get(mood.lower(), []) or [None])

        movie_type = 'tv-series' if self._is_tv_series_request(user_message) else 'movie'

        # Используем обновленный movie_agent с улучшенной логикой
        movies = self.movie_agent.recommend_movies(
            genre_name=genre,
            year=year,
            actor=actor,
            director=director,
            studio=studio,
            country=country,
            min_imdb_rating=min_rating,
            limit=count,
            movie_type=movie_type,
            query=user_message  # Передаем оригинальный запрос для улучшенной логики
        )

        if not movies or (isinstance(movies, dict) and "error" in movies):
            return {
                "response": "Не удалось найти фильмы по вашему запросу. Попробуйте изменить жанр, год или страну.",
                "needs_clarification": True,
                "parameters": params
            }

        if actor:
            session['last_actor'] = actor
        session['last_movies'] = movies
        session['last_params'] = params

        if count == 1 and len(movies) == 1:
            response_text = self._generate_single(movies[0])
            return {
                "response": response_text,
                "needs_clarification": False,
                "parameters": params,
                "movie": movies[0],
                "movies_list": None
            }
        else:
            response_text = self._generate_list(movies, clickable=True)
            return {
                "response": response_text,
                "needs_clarification": False,
                "parameters": params,
                "movies_list": movies[:count]
            }