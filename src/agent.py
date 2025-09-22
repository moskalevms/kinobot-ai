import os
import random
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv

from src.client.tmdb_client import TMDBClient
from src.client.omdb_client import OMDBClient
from src.client.kinopoisk_client import KinopoiskClient

# Загружаем .env один раз при импорте
load_dotenv()

# Получаем прокси из .env
TMDB_PROXIES = {
    "http": os.getenv("TMDB_HTTP_PROXY", "").strip() or None,
    "https": os.getenv("TMDB_HTTPS_PROXY", "").strip() or None
}
# Убираем пустые ключи
TMDB_PROXIES = {k: v for k, v in TMDB_PROXIES.items() if v}

class MovieAgent:
    def __init__(self, use_api=True):
        """
        Инициализация агента
        :param use_api: использовать ли API (True) или локальный датасет (False)
        """
        self.use_api = use_api
        self.data_path = Path(__file__).parent.parent / "data" / "processed" / "imdb" / "imdb_top_1000.csv"

        if use_api:
            # 👇 Передаём прокси ТОЛЬКО в TMDBClient
            self.tmdb_client = TMDBClient(proxies=TMDB_PROXIES)
            # 👇 OMDB и Kinopoisk — без прокси
            self.omdb_client = OMDBClient()
            self.kinopoisk_client = KinopoiskClient()
            self.genres = self._load_genres_from_tmdb()
        else:
            self.df = self._load_data_from_csv()
            self.genres = self._load_genres_from_csv()

    def _load_data_from_csv(self):
        """Загрузка данных из CSV"""
        df = pd.read_csv(self.data_path)

        # Базовая предобработка
        df['Genre'] = df['Genre'].str.lower()
        df['Series_Title'] = df['Series_Title'].str.title()
        df['Released_Year'] = pd.to_numeric(df['Released_Year'], errors='coerce')

        return df

    def _load_genres_from_csv(self):
        """Загрузка жанров из CSV"""
        all_genres = self.df['Genre'].str.split(',').explode().str.strip().unique()
        return {i: genre for i, genre in enumerate(all_genres)}

    def _load_genres_from_tmdb(self):
        """Загрузка жанров из TMDB"""
        genres = self.tmdb_client.get_genres()
        return {genre['id']: genre['name'] for genre in genres}

    def _recommend_from_csv(self, genre_name=None, year=None):
        """Рекомендация из локального датасета"""
        filtered_df = self.df.copy()

        if genre_name:
            genre_lower = genre_name.lower()
            filtered_df = filtered_df[filtered_df['Genre'].str.contains(genre_lower, na=False, case=False)]

        if year:
            filtered_df = filtered_df[filtered_df['Released_Year'] == year]

        if filtered_df.empty:
            return {"error": "По вашему запросу ничего не найдено"}

        random_movie = filtered_df.sample(1).iloc[0]

        return {
            "title": random_movie['Series_Title'],
            "year": str(random_movie['Released_Year']),
            "genre": random_movie['Genre'],
            "rating": random_movie['IMDB_Rating'],
            "description": random_movie['Overview'],
            "source": "local dataset"
        }

    def _recommend_from_api(self, genre_name=None, year=None):
        """Рекомендация из API (сначала Kinopoisk, затем TMDB/OMDB)"""
        # Сначала пробуем Kinopoisk
        kinopoisk_recommendation = self._recommend_from_kinopoisk(genre_name, year)
        if kinopoisk_recommendation:
            return kinopoisk_recommendation

        # Если Kinopoisk не дал результатов, используем старую логику (TMDB + OMDB)
        print("Kinopoisk не нашел фильмов, пробуем TMDB...")

        # Преобразуем название жанра в ID для TMDB
        genre_id = None
        if genre_name:
            genre_name_lower = genre_name.lower()
            for gid, name in self.genres.items():
                if genre_name_lower in name.lower():
                    genre_id = gid
                    break

        # Ищем фильмы по критериям в TMDB
        result = self.tmdb_client.search_movies(genre=genre_id, year=year)

        if not result or 'results' not in result or not result['results']:
            return {"error": "По вашему запросу ничего не найдено"}

        # ... (остальная логика метода _recommend_from_api остается без изменений)
        # Выбираем случайный фильм из результатов
        movies = result['results']
        random_movie = random.choice(movies)

        # Пытаемся получить детальную информацию из OMDB
        omdb_data = None
        if random_movie.get('title'):
            omdb_data = self.omdb_client.search_movies(title=random_movie['title'], year=year)

        # Если OMDB не ответил, используем данные из TMDB
        if not omdb_data:
            movie_details = self.tmdb_client.get_movie_details(random_movie['id'])

            if not movie_details:
                return {"error": "Не удалось получить информацию о фильме"}

            # Форматируем жанры
            movie_genres = ", ".join([genre['name'] for genre in movie_details.get('genres', [])])

            return {
                "title": movie_details.get('title', 'Неизвестно'),
                "year": movie_details.get('release_date', '')[:4] if movie_details.get(
                    'release_date') else 'Неизвестно',
                "genre": movie_genres,
                "rating": movie_details.get('vote_average', 0),
                "description": movie_details.get('overview', 'Описание отсутствует'),
                "source": "TMDB API"
            }

        # Используем данные из OMDB
        return {
            "title": omdb_data.get('Title', 'Неизвестно'),
            "year": omdb_data.get('Year', 'Неизвестно'),
            "genre": omdb_data.get('Genre', 'Неизвестно'),
            "rating": omdb_data.get('imdbRating', 0),
            "description": omdb_data.get('Plot', 'Описание отсутствует'),
            "source": "OMDB API"
        }

    def _recommend_from_kinopoisk(self, genre_name=None, year=None):
        """Рекомендация из Kinopoisk API"""
        try:
            # Передаем название жанра напрямую, API принимает строку
            result = self.kinopoisk_client.search_movies(genre=genre_name, year=year)

            if not result or 'docs' not in result or not result['docs']:
                return None  # Возвращаем None, если ничего не найдено

            # Выбираем случайный фильм из результатов
            movies = result['docs']
            random_movie = random.choice(movies)

            # Форматируем жанры из списка объектов в строку
            movie_genres = ", ".join([g['name'] for g in random_movie.get('genres', [])])

            # Возвращаем данные в унифицированном формате
            return {
                "title": random_movie.get('name', 'Неизвестно'),
                "year": str(random_movie.get('year', 'Неизвестно')),
                "genre": movie_genres,
                "rating": random_movie.get('rating', {}).get('kp', 0),  # Рейтинг Кинопоиска
                "description": random_movie.get('description', 'Описание отсутствует'),
                "source": "Kinopoisk API"
            }

        except Exception as e:
            print(f"Ошибка при обращении к Kinopoisk API: {e}")
            return None

    def recommend_movie(self, genre_name=None, year=None):
        """
        Рекомендация случайного фильма по критериям
        :param genre_name: название жанра (опционально)
        :param year: год выпуска (опционально)
        :return: словарь с информацией о фильме
        """
        if self.use_api:
            try:
                return self._recommend_from_api(genre_name, year)
            except Exception as e:
                print(f"Ошибка при обращении к API: {e}. Использую локальный датасет.")
                return self._recommend_from_csv(genre_name, year)
        else:
            return self._recommend_from_csv(genre_name, year)


# Обновленная функция для запуска в консоли
def run_console_agent(use_api=True):
    # Инициализируем агента
    agent = MovieAgent(use_api=use_api)

    print("Добро пожаловать в Кинобот! Давайте подберем фильм на вечер.")
    print(f"Режим работы: {'API' if use_api else 'Локальный датасет'}")
    print("Вы можете указать жанр и/или год, или просто нажать Enter для случайного выбора.")

    # Покажем доступные жанры
    print("\nДоступные жанры:")
    genres_list = list(agent.genres.values())
    print(", ".join(genres_list[:10]) + ("..." if len(genres_list) > 10 else ""))

    while True:
        try:
            # Запрос критериев у пользователя
            genre_input = input("\nВведите жанр: ").strip()
            year_input = input("Введите год (например, 2020): ").strip()

            # Преобразуем входные данные
            genre = genre_input if genre_input else None
            year = int(year_input) if year_input else None

            # Получаем рекомендацию
            recommendation = agent.recommend_movie(genre_name=genre, year=year)

            # Выводим результат
            if "error" in recommendation:
                print(recommendation["error"])
            else:
                print(f"\n🎬 Как насчёт: {recommendation['title']} ({recommendation['year']})")
                print(f"📀 Жанр: {recommendation['genre']}")
                print(f"⭐ Рейтинг: {recommendation['rating']}/10")
                print(f"📖 Описание: {recommendation['description']}")
                print(f"ℹ️  Источник: {recommendation.get('source', 'неизвестно')}")

            # Спрашиваем, продолжить ли
            continue_input = input("\nХотите еще один вариант? (y/n): ").strip().lower()
            if continue_input != 'y':
                print("Приятного просмотра!")
                break

        except ValueError:
            print("Пожалуйста, введите корректный год.")
        except KeyboardInterrupt:
            print("\nДо свидания!")
            break
        except Exception as e:
            print(f"Произошла ошибка: {e}")
            break


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Kinobot AI - подбор фильмов')
    parser.add_argument('--local', action='store_true', help='Использовать локальный датасет вместо API')

    args = parser.parse_args()

    run_console_agent(use_api=not args.local)