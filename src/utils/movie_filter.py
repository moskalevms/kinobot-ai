from typing import List, Dict, Optional

def filter_movies_by_quality(
    movies: List[Dict],
    year: Optional[int] = None,
    min_rating: float = 6.0,
    min_votes_override: Optional[int] = None
) -> List[Dict]:
    current_year = 2025

    def _calculate_min_votes(y: Optional[int]) -> int:
        if not y:
            return 10000
        diff = current_year - y
        if diff <= 1:
            return 1000
        elif diff <= 3:
            return 5000
        elif diff <= 10:
            return 10000
        else:
            return 10000

    min_votes = min_votes_override if min_votes_override is not None else _calculate_min_votes(year)
    filtered = []

    for movie in movies:
        rating = movie.get('rating', {})
        votes = movie.get('votes', {})
        imdb_rating = rating.get('imdb')
        kp_rating = rating.get('kp')
        imdb_votes = votes.get('imdb') or 0
        kp_votes = votes.get('kp') or 0

        best_rating = imdb_rating if imdb_rating is not None else kp_rating
        # Берём максимум из доступных голосов:
        best_votes = max(imdb_votes, kp_votes)

        if best_rating is None or best_rating < min_rating:
            continue
        if best_votes < min_votes:
            continue
        if not movie.get('name') or not movie.get('year'):
            continue

        filtered.append(movie)
    return filtered