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


def _env_bool(name: str, default: str = 'false') -> bool:
    """Чтение булевой переменной окружения: регистр и пробелы по краям не важны."""
    return os.getenv(name, default).strip().lower() == 'true'


def _env_str(name: str) -> str:
    """Чтение строковой переменной окружения: отсутствие -> '', пробелы снимаются."""
    return (os.getenv(name) or '').strip()


# --- Epic B: оценки Rotten Tomatoes через OMDb API ---
# Лицензия OMDb: CC BY-NC 4.0 (только некоммерческое использование);
# решение зафиксировано комментарием в .env.example.
# Пустой OMDB_API_KEY — валидное состояние: RT-функциональность выключена.
# Каноническая проверка активности Epic B — только rt_scores_enabled().
OMDB_API_KEY = _env_str('OMDB_API_KEY')
ENABLE_RT_SCORES = _env_bool('ENABLE_RT_SCORES')


def rt_scores_enabled() -> bool:
    """Feature flag Epic B (Rotten Tomatoes через OMDb).

    Возвращает True только при ENABLE_RT_SCORES=true (регистронезависимо)
    И непустом OMDB_API_KEY. Переменные окружения читаются в момент вызова,
    чтобы тесты могли переключать флаг через monkeypatch.setenv без
    перезагрузки модуля (importlib.reload); в продакшене env фиксируется до
    старта процесса, поэтому расхождение с константами модуля недостижимо.
    """
    return _env_bool('ENABLE_RT_SCORES') and bool(_env_str('OMDB_API_KEY'))
