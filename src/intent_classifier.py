# src/intent_classifier.py
import re
import json
import logging
import os
from typing import Dict, Any, Optional

from .llm_router import LLMRouter

logger = logging.getLogger(__name__)


class IntentClassifier:
    def __init__(self, llm_router: LLMRouter, prompts_dir: str):
        self.llm_router = llm_router
        self.prompts_dir = prompts_dir
        logger.info(f"IntentClassifier инициализирован с папкой промптов: {prompts_dir}")

    def _load_prompt(self, filename: str) -> str:
        """Загрузка промпта из файла"""
        path = os.path.join(self.prompts_dir, filename)
        logger.info(f"Пытаемся загрузить промпт: {path}")

        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                logger.info(f"Промпт {filename} успешно загружен")
                return content
        except Exception as e:
            logger.error(f"Ошибка загрузки промпта {filename}: {e}")
            return ""

    # Добавляем в метод classify_with_llm обработку десятилетий
    def classify_with_llm(self, message: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Классификация с улучшенной обработкой десятилетий"""
        try:
            system_prompt = self._load_prompt('parameter_extraction_prompt.txt')
            if not system_prompt:
                logger.warning("Не удалось загрузить системный промпт, используем упрощенную классификацию")
                return self._classify_fallback(message)

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message}
            ]

            response = self.llm_router.call_llm(messages, max_tokens=250)
            if not response:
                logger.warning("LLM не вернул ответ, используем упрощенную классификацию")
                return self._classify_fallback(message)

            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if not json_match:
                logger.warning(f"Не удалось извлечь JSON из ответа LLM: {response}")
                return self._classify_fallback(message)

            json_str = json_match.group(0)
            params = json.loads(json_str)

            # Приведение movie_type (если LLM вернул строку)
            if params.get("movie_type") not in ["movie", "tv-series"]:
                params["movie_type"] = "movie"  # fallback

            # Приведение типов и улучшенная обработка годов
            if params.get("count") is not None:
                try:
                    params["count"] = int(params["count"])
                except (TypeError, ValueError):
                    params["count"] = None

            # Обработка года с учетом десятилетий
            year = params.get("year")
            if year is None:
                # Пытаемся извлечь десятилетие из сообщения
                decade_match = re.search(r'(\d{3})0-х', message)
                if decade_match:
                    decade = int(decade_match.group(1) + "0")
                    params["year"] = decade
                    logger.info(f"Извлечено десятилетие: {decade}0-е")

            if params.get("year") is not None:
                try:
                    params["year"] = int(params["year"])
                except (TypeError, ValueError):
                    params["year"] = None

            if params.get("min_rating") is not None:
                try:
                    params["min_rating"] = float(params["min_rating"])
                except (TypeError, ValueError):
                    params["min_rating"] = None

            logger.info(f"LLM классификация успешна: {params}")
            return params

        except Exception as e:
            logger.error(f"Ошибка классификации LLM: {e}")
            return self._classify_fallback(message)

    # В методе _classify_fallback добавить/обновить маппинг для аниме:
    def _classify_fallback(self, message: str) -> Dict[str, Any]:
        """Упрощенная классификация с улучшенной обработкой десятилетий"""
        message_lower = message.lower()
        params = self._get_default_params()

        # УЛУЧШЕННАЯ обработка десятилетий - текстовые описания
        decade_mapping = {
            'девяностых': 1990, '90-х': 1990, '90-е': 1990, 'девяностые': 1990,
            'восьмидесятых': 1980, '80-х': 1980, '80-е': 1980, 'восьмидесятые': 1980,
            'семидесятых': 1970, '70-х': 1970, '70-е': 1970, 'семидесятые': 1970,
            'шестидесятых': 1960, '60-х': 1960, '60-е': 1960, 'шестидесятые': 1960,
            'пятидесятых': 1950, '50-х': 1950, '50-е': 1950, 'пятидесятые': 1950,
            'двухтысячных': 2000, '2000-х': 2000, '2000-е': 2000, 'нулевых': 2000, 'нулевые': 2000,
            'десятых': 2010, '2010-х': 2010, '2010-е': 2010, 'десятые': 2010,
            'двадцатых': 2020, '2020-х': 2020, '2020-е': 2020, 'двадцатые': 2020
        }

        # Сначала проверяем текстовые описания
        decade_found = False
        for decade_text, decade_year in decade_mapping.items():
            if decade_text in message_lower:
                params["year"] = decade_year
                decade_found = True
                logger.info(f"Извлечено десятилетие текстовое: {decade_text} → {decade_year}")
                break

        # Если текстовое не нашли, проверяем числовое (как было)
        if not decade_found:
            decade_match = re.search(r'(\d{3})0-х', message_lower)
            if decade_match:
                decade = int(decade_match.group(1) + "0")
                params["year"] = decade
                logger.info(f"Извлечено десятилетие числовое: {decade}0-е")

        # Обработка количества в запросе "топ N фильмов"
        count_match = re.search(r'топ\s*(\d+)', message_lower)
        if count_match:
            try:
                params["count"] = int(count_match.group(1))
                logger.info(f"Извлечено количество: {params['count']}")
            except (TypeError, ValueError):
                params["count"] = None

        # Обработка стран
        if 'французск' in message_lower:
            params["country"] = "Франция"
        elif 'американск' in message_lower or 'сша' in message_lower:
            params["country"] = "США"
        elif 'британск' in message_lower or 'английск' in message_lower:
            params["country"] = "Великобритания"
        elif 'русск' in message_lower:
            params["country"] = "Россия"

        # Обработка жанров (включая исключаемые)
        genre_mapping = {
            'комеди': "комедия",
            'драм': "драма",
            'боевик': "боевик",
            'экшн': "боевик",
            'триллер': "триллер",
            'ужас': "ужасы",
            'фантастик': "фантастика",
            'мелодрам': "мелодрама",
            'приключени': "приключения",
            'фэнтези': "фэнтези",
            'детектив': "детектив",
            'криминал': "криминал",
            'военн': "военный",
            'историческ': "исторический",
            'семейн': "семейный",
            'мульт': "мультфильм",
            'аниме': "аниме",  # ← Явно указываем аниме, а не анимацию
            'анимац': "аниме",  # ← На случай опечаток
            # Исключаемые жанры (но если запрошены явно - разрешаем)
            'мюзикл': "мюзикл",
            'концерт': "концерт",
            'документальн': "документальный",
            'короткометраж': "короткометражка",
            'биографи': "биография",
            'артхаус': "артхаус",
            'эротик': "эротика",
            'для взрослых': "для взрослых",
            'adult': "для взрослых"
        }

        for keyword, genre in genre_mapping.items():
            if keyword in message_lower:
                params["genre"] = genre
                break

        if any(word in message_lower for word in
               ['сериал', 'сериалы', 'сериалов', 'series', 'шоу', 'телешоу']) and 'аниме' not in message_lower:
            params["movie_type"] = "tv-series"

        # Простая логика для настроения
        if any(word in message_lower for word in ['грустн', 'весел', 'настроен', 'чувств', 'устал', 'скучно']):
            params["mood"] = "грустный" if 'грустн' in message_lower else "веселый"

        # Простая логика для топа
        if any(word in message_lower for word in ['топ', 'лучш', 'рейтинг']):
            params["min_rating"] = 7.0

        # Простая логика для информации о фильме
        if any(word in message_lower for word in ['расскажи', 'информац', 'сюжет']):
            params["intent"] = "info"

        logger.info(f"Fallback классификация: {params}")
        return params

    def _get_default_params(self) -> Dict[str, Any]:
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
            "min_rating": None,
            "movie_type": "movie"
        }