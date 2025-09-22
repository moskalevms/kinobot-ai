# kinopoisk_client.py

import requests
from config import KINOPOISK_API_KEY, KINOPOISK_URL

class KinopoiskClient:
    def __init__(self):
        self.api_key = KINOPOISK_API_KEY
        self.base_url = f"{KINOPOISK_URL.rstrip('/')}/v1.4/movie"  # Исправленный URL
        self.session = requests.Session()
        # Устанавливаем заголовок авторизации, как того требует API
        self.session.headers.update({
            'X-API-KEY': self.api_key,
            'Content-Type': 'application/json'
        })

    def search_movies(self, genre=None, year=None, limit=50):
        """
        Поиск фильмов в Kinopoisk API по жанру и году.
        :param genre: Название жанра (строка, например, "драма")
        :param year: Год выпуска (целое число)
        :param limit: Максимальное количество результатов
        :return: Словарь с данными API или None в случае ошибки
        """
        params = {
            'limit': limit,
            'page': 1,
            'selectFields': ['name', 'year', 'genres', 'rating', 'description', 'poster'],
            'sortField': 'rating.kp',
            'sortType': -1  # Сортировка по убыванию рейтинга
        }

        # Фильтрация по году
        if year:
            params['year'] = year

        # Фильтрация по жанру
        if genre:
            # В API жанры передаются как массив имен
            # Пример: genres.name=драма
            params['genres.name'] = genre

        try:
            response = self.session.get(self.base_url, params=params)
            response.raise_for_status()
            data = response.json()

            # API возвращает объект с полем 'docs', содержащим список фильмов
            if 'docs' in data and len(data['docs']) > 0:
                return data
            else:
                print("Kinopoisk API: По вашему запросу ничего не найдено.")
                return None

        except requests.exceptions.RequestException as e:
            print(f"Ошибка при запросе к Kinopoisk API: {e}")
            return None

    def get_movie_details(self, movie_id):
        """
        Получение подробной информации о фильме по его ID.
        Этот метод может понадобиться для будущих улучшений.
        """
        url = f"{self.base_url}/{movie_id}"
        try:
            response = self.session.get(url)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Ошибка при запросе к Kinopoisk API: {e}")
            return None