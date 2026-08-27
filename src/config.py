# src/config.py
import os
from datetime import date
from dotenv import load_dotenv

load_dotenv()

KINOPOISK_API_KEY = os.getenv('KINOPOISK_API_KEY')
KINOPOISK_URL = 'https://api.kinopoisk.dev'

# Текущий год — единственный источник для всего проекта
CURRENT_YEAR = date.today().year

MIN_VOTES_IMDB = int(os.getenv('MIN_VOTES_IMDB', '5000'))
MIN_VOTES_KP = int(os.getenv('MIN_VOTES_KP', '10000'))

CACHE_TTL = int(os.getenv('CACHE_TTL', '45'))
