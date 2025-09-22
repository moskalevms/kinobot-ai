# src/client/tmdb_client.py
import os
import requests

class TMDBClient:
    def __init__(self, proxies=None):
        """
        Инициализация клиента TMDB
        :param proxies: словарь прокси вида {'http': '...', 'https': '...'}, опционально
        """
        self.api_key = os.getenv("TMDB_API_KEY")
        self.base_url = "https://api.themoviedb.org/3"
        self.proxies = proxies  # сохраняем прокси, если переданы

    def _make_request(self, url, params=None):
        """
        Вспомогательный метод для выполнения запросов
        """
        params = params or {}
        params['api_key'] = self.api_key

        try:
            # 👇 Используем прокси, если они переданы
            response = requests.get(
                url,
                params=params,
                proxies=self.proxies,
                timeout=(20, 30)
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"Ошибка TMDB API: {e}")
            return None

    def get_genres(self):
        """
        Получение списка жанров
        """
        url = f"{self.base_url}/genre/movie/list"
        data = self._make_request(url, {"language": "ru-RU"})
        return data.get("genres", []) if data else []

    def search_movies(self, genre=None, year=None):
        """
        Поиск фильмов по жанру и году
        """
        url = f"{self.base_url}/discover/movie"
        params = {
            "sort_by": "popularity.desc",
            "language": "ru-RU"
        }
        if genre:
            params["with_genres"] = genre
        if year:
            params["year"] = year

        return self._make_request(url, params)

    def get_movie_details(self, movie_id):
        """
        Получение детальной информации о фильме
        """
        url = f"{self.base_url}/movie/{movie_id}"
        return self._make_request(url, {"language": "ru-RU"})