import requests
from config import OMDB_API_KEY, OMDB_BASE_URL


class OMDBClient:
    def __init__(self):
        self.api_key = OMDB_API_KEY
        self.base_url = OMDB_BASE_URL
        self.session = requests.Session()

    def search_movies(self, title=None, year=None, plot="short"):
        """Поиск фильмов в OMDB API"""
        params = {
            'apikey': self.api_key,
            'plot': plot
        }

        if title:
            params['t'] = title
        if year:
            params['y'] = year

        try:
            response = self.session.get(self.base_url, params=params)
            response.raise_for_status()
            data = response.json()

            if data.get('Response') == 'True':
                return data
            else:
                print(f"OMDB API error: {data.get('Error')}")
                return None

        except requests.exceptions.RequestException as e:
            print(f"Ошибка при запросе к OMDB API: {e}")
            return None

    def get_movie_by_id(self, imdb_id):
        """Получение фильма по IMDB ID"""
        params = {
            'apikey': self.api_key,
            'i': imdb_id,
            'plot': 'full'
        }

        try:
            response = self.session.get(self.base_url, params=params)
            response.raise_for_status()
            data = response.json()

            if data.get('Response') == 'True':
                return data
            else:
                print(f"OMDB API error: {data.get('Error')}")
                return None

        except requests.exceptions.RequestException as e:
            print(f"Ошибка при запросе к OMDB API: {e}")
            return None