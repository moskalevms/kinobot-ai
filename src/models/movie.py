from dataclasses import dataclass
from typing import Optional, List, Dict, Any


@dataclass
class MovieRating:
    kp: Optional[float] = None
    imdb: Optional[float] = None
    filmCritics: Optional[float] = None
    russianFilmCritics: Optional[float] = None
    expected: Optional[float] = None  # Заменили await на expected


@dataclass
class MovieVotes:
    kp: Optional[int] = None
    imdb: Optional[int] = None
    filmCritics: Optional[int] = None
    russianFilmCritics: Optional[int] = None
    expected: Optional[int] = None  # Заменили await на expected


@dataclass
class Country:
    name: str


@dataclass
class Genre:
    name: str


@dataclass
class Movie:
    id: int
    name: str
    rating: Optional[MovieRating] = None
    votes: Optional[MovieVotes] = None
    year: Optional[int] = None
    genres: Optional[List[Genre]] = None
    countries: Optional[List[Country]] = None
    description: Optional[str] = None
    poster: Optional[Dict[str, str]] = None

    def get_best_rating(self) -> float:
        """Возвращает лучший доступный рейтинг (IMDB приоритетнее)"""
        if self.rating and self.rating.imdb:
            return self.rating.imdb
        elif self.rating and self.rating.kp:
            return self.rating.kp
        return 0.0

    def get_votes_count(self) -> int:
        """Возвращает количество голосов (IMDB приоритетнее)"""
        if self.votes and self.votes.imdb:
            return self.votes.imdb
        elif self.votes and self.votes.kp:
            return self.votes.kp
        return 0

    def has_us_production(self) -> bool:
        """Проверяет, является ли фильм американским"""
        if not self.countries:
            return False
        us_keywords = ['сша', 'америка', 'usa', 'united states', 'соединённые штаты']
        return any(country.name.lower() in us_keywords for country in self.countries)

    def get_genre_names(self) -> List[str]:
        """Возвращает список названий жанров"""
        if not self.genres:
            return []
        return [genre.name for genre in self.genres]

    def get_country_names(self) -> List[str]:
        """Возвращает список названий стран"""
        if not self.countries:
            return []
        return [country.name for country in self.countries]