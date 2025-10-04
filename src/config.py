import os
from dotenv import load_dotenv

load_dotenv()

TMDB_API_KEY = os.getenv('TMDB_API_KEY')
OMDB_API_KEY = os.getenv('OMDB_API_KEY')
KINOPOISK_API_KEY = os.getenv('KINOPOISK_API_KEY')
TMDB_BASE_URL = 'https://api.themoviedb.org/3'
OMDB_BASE_URL = 'http://www.omdbapi.com/'
KINOPOISK_URL = 'https://api.kinopoisk.dev'

MIN_VOTES_IMDB = int(os.getenv('MIN_VOTES_IMDB', '1000'))
MIN_VOTES_KP = int(os.getenv('MIN_VOTES_KP', '2000'))