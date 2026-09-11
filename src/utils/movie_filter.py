# src/utils/movie_filter.py
from typing import List, Dict, Optional, Set, Tuple
import logging

logger = logging.getLogger(__name__)

# Жанры, исключаемые из выдачи по умолчанию. Единый перечень для всех
# контуров фильтрации; если пользователь явно запрашивает такой жанр,
# он передаётся в allowed_excluded_genres.
EXCLUDED_GENRES = {
    'мюзикл', 'концерт', 'документальный', 'документалка',
    'короткометражка', 'короткометражный', 'биография',
    'артхаус', 'реалити-тв', 'ток-шоу', 'церемония',
    'эротика', 'для взрослых', 'adult', '18+', 'спорт', 'спортивный',
    'новости', 'новостной',
}

# Жанры «музыкального» контента. Кинопоиск часто помечает концерты,
# лайв-выступления и музыкальные документалки только жанром «музыка»
# (например, «Metallica: Live Shit», «Режиссёр Мишель Гондри в работе»),
# поэтому по жанрам «концерт»/«документальный» они не отсеиваются.
# Тайтл, у которого ВСЕ жанры входят в этот набор, считается
# концертным/музыкальным контентом и исключается по умолчанию.
MUSIC_ONLY_GENRES = {'музыка', 'концерт', 'мюзикл'}

# Стендап-выступления. Кинопоиск не выделяет стендап в отдельный жанр:
# такие тайтлы помечаются «комедией» (иногда в сочетании с «музыкой»),
# поэтому детекция опирается на жанры + маркеры в названии/описании
# («стендап», «выступление комика, записанное в...»). Тайтлы с жанром
# «документальный» сюда не относятся — их отсеивает список
# исключаемых жанров.
STANDUP_CONTENT_GENRES = {'комедия', 'музыка'}

# Явные упоминания стендапа в названии/описании
STANDUP_EXPLICIT_MARKERS = (
    'стендап', 'стенд-ап', 'стенд ап', 'stand-up', 'standup', 'stand up',
)
# Маркеры описания записанного выступления
STANDUP_PERFORMANCE_MARKERS = ('выступлен', 'концерт')
# Маркеры комика/записи/сцены — в сочетании с маркером выступления
STANDUP_COMPANION_MARKERS = ('комик', 'записан', 'записыв', 'сцене', 'сцену')


# Страны с приоритетной выдачей (англоязычные)
HIGH_PRIORITY_COUNTRIES = {
    'сша', 'usa', 'united states', 'канада', 'canada',
    'великобритания', 'uk', 'united kingdom',
}

# --- Параметры рейтинга кинокритиков (фаза 0, Epic A) ---
# kinopoisk.dev возвращает rating.filmCritics в шкале 0–10 (НЕ процент).
# Значение 0 или None означает «нет данных», а не провальную оценку:
# демотивация в таком случае не применяется (подтверждено исследованием
# docs/research/research_rotten_tomatoes.md).
CRITICS_SHRINK_FACTOR = 0.2      # вес шринка базовой оценки к консенсусу критиков
CRITICS_MIN_VOTES_SHRINK = 10    # минимум голосов критиков для применения шринка
CRITICS_MIN_VOTES_TIER = 20      # минимум голосов критиков для ступеней ±
CRITICS_TIER_HIGH_FC = 8.0       # порог «одобрено критиками» (Certified Fresh)
CRITICS_TIER_LOW_FC = 3.0        # порог «критического провала»
CRITICS_TIER_BONUS = 0.3         # величина ступени
# Суммарный кламп ступеней: ступень fc (A1) и ступень Tomatometer (B6)
# складываются и ограничиваются ±0.3 СУММАРНО (две однонаправленные
# ступени не удваиваются, разнонаправленные — гасят друг друга).
CRITICS_TIER_CLAMP = 0.3
# Границы итоговой оценки (шкала Кинопоиска/IMDb)
RATING_MIN = 0.0
RATING_MAX = 10.0

# --- Параметры Tomatometer (фаза 1, Epic B, B6) ---
# rt_score из OMDb — процент «свежести» 0–100, шкала оценки ранжирования —
# 0–10, поэтому нормализация rt/10 обязательна (смешение единиц недопустимо).
# Значение None означает «нет данных» (нет IMDb ID, сбой, выключен флаг);
# rt_score == 0 — валидная провальная оценка (0% «свежести»), не «нет данных».
RT_SHRINK_FACTOR = 0.2     # вес шринка оценки s1 к консенсусу критиков RT
RT_TIER_HIGH = 75          # порог «одобрено критиками RT» (Certified Fresh)
RT_TIER_LOW = 40           # порог «провала» по мнению критиков RT
RT_TIER_BONUS = 0.3        # величина ступени Tomatometer
RT_PERCENT_SCALE = 10.0    # делитель нормализации: 0–100 → шкала 0–10


def get_country_priority(movie: Dict) -> int:
    """Приоритет фильма по странам производства.

    0 — все страны входят в приоритетный перечень;
    1 — копродукция: приоритетные страны есть, но не все;
    2 — приоритетных стран нет (включая фильм без стран).
    """
    countries = movie.get('countries', [])
    if not countries:
        return 2

    country_names = {c.get('name', '').lower() for c in countries if isinstance(c, dict)}
    matches = country_names & HIGH_PRIORITY_COUNTRIES
    if not matches:
        return 2
    if matches == country_names:
        return 0
    return 1


def is_russian_content(movie: Dict) -> bool:
    countries = movie.get('countries', [])
    if not countries:
        return False

    country_names = {c.get('name', '').lower() for c in countries if isinstance(c, dict)}
    russian_keywords = {'россия', 'russia', 'российская федерация', 'russian federation', 'ссср', 'soviet union'}
    return any(rk in country_names for rk in russian_keywords)


def _get_base_weighted_rating(movie: Dict, is_russian_search: bool = False) -> float:
    """Базовая оценка фильма без учёта кинокритиков.

    Прежняя логика взвешенного рейтинга: российская ветка — КП, иначе
    IMDb*0.9; иностранная ветка — IMDb, иначе КП*0.8; 0.0 при отсутствии
    обоих рейтингов.
    """
    rating = movie.get('rating', {})
    imdb_rating = rating.get('imdb')
    kp_rating = rating.get('kp')

    if is_russian_search or is_russian_content(movie):
        if kp_rating is not None:
            return kp_rating
        elif imdb_rating is not None:
            return imdb_rating * 0.9
        else:
            return 0.0
    else:
        if imdb_rating is not None:
            return imdb_rating
        elif kp_rating is not None:
            return kp_rating * 0.8
        else:
            return 0.0


def _get_critics_data(movie: Dict) -> Optional[Tuple[float, int]]:
    """Данные кинокритиков: (рейтинг fc в шкале 0–10, число голосов).

    Возвращает None, когда данных нет: fc отсутствует/None/0 (в любом
    числовом представлении, включая строковое '0'/'0.0') либо голосов
    критиков меньше CRITICS_MIN_VOTES_SHRINK. fc == 0 у источника означает
    «рецензий нет», а не провальную оценку, — демотивация за такое значение
    недопустима. Нечисловые значения также трактуем как «нет данных».
    """
    rating = movie.get('rating') or {}
    votes = movie.get('votes') or {}
    fc = rating.get('filmCritics')
    fc_votes = votes.get('filmCritics') or 0

    # Сначала приводим к числу и только потом проверяем ноль: API может
    # вернуть fc строкой ('0', '0.0'), и сравнение строки с нулём ('0' == 0)
    # дало бы False — «нет данных» просочилось бы как провальная оценка.
    # Проверка None до приведения нужна только для mypy: float(None) и так
    # ловится TypeError ниже.
    if fc is None:
        return None
    try:
        fc_value = float(fc)
        fc_votes_value = int(fc_votes)
    except (TypeError, ValueError):
        return None
    if fc_value == 0:
        return None
    if fc_votes_value < CRITICS_MIN_VOTES_SHRINK:
        return None
    return fc_value, fc_votes_value


def extract_critics_fields(movie: Dict) -> Tuple[Optional[float], Optional[int]]:
    """Поля кинокритиков для итогового словаря фильма (фаза 0, Epic A, A2).

    Возвращает пару (critics_rating, critics_votes):
    - critics_rating — rating.filmCritics в шкале 0–10 либо None, если
      данных нет: поле отсутствует, равно None/0 (в любом представлении,
      включая строковое '0'/'0.0') или нечисловое — семантика «нет данных»
      согласована с _get_critics_data (A1);
    - critics_votes — votes.filmCritics (включая 0) либо None, если поле
      отсутствует или нечисловое.

    В отличие от _get_critics_data, порог голосов для шринка здесь не
    применяется: поля просто отражают факт наличия данных в ответе API
    (используются для выдачи, триггеров и отладки).
    """
    rating = movie.get('rating') or {}
    votes = movie.get('votes') or {}
    fc = rating.get('filmCritics')
    fc_votes = votes.get('filmCritics')

    fc_value: Optional[float] = None
    if fc is not None:
        try:
            fc_value = float(fc)
        except (TypeError, ValueError):
            fc_value = None
    # Ноль (в т.ч. строковый) у источника означает «рецензий нет»
    critics_rating: Optional[float] = None
    if fc_value is not None and fc_value != 0:
        critics_rating = fc_value

    critics_votes: Optional[int] = None
    if fc_votes is not None:
        try:
            critics_votes = int(fc_votes)
        except (TypeError, ValueError):
            critics_votes = None

    return critics_rating, critics_votes


def extract_imdb_id(movie: Dict) -> Optional[str]:
    """IMDb ID фильма из ответа kinopoisk.dev (фаза 1, Epic B, B3).

    Join-ключ для обогащения оценками Rotten Tomatoes через OMDb (B5):
    kinopoisk.dev отдаёт внешние идентификаторы в поле externalId
    ({"imdb": "tt0111161", "tmdb": ..., "trakt": ...}).

    Возвращает непустую строку IMDb ID (с обрезанными пробелами) либо
    None, если данных нет: externalId отсутствует, не является
    словарём, imdb равен None/не-строке/пустой строке. Покрытие
    externalId.imdb — ~69% базы, поэтому None — штатная ситуация
    (незаметная деградация: RT-бейджа у такого фильма просто нет).
    """
    external = movie.get('externalId')
    if not isinstance(external, dict):
        return None
    imdb = external.get('imdb')
    if not isinstance(imdb, str):
        return None
    imdb = imdb.strip()
    return imdb or None


def get_critics_tier(movie: Dict) -> float:
    """Ступень по rating.filmCritics (фаза 0, A1): +0.3 / −0.3 / 0.0.

    +CRITICS_TIER_BONUS при fc >= CRITICS_TIER_HIGH_FC и votes >=
    CRITICS_MIN_VOTES_TIER («одобрено критиками»), −CRITICS_TIER_BONUS при
    fc <= CRITICS_TIER_LOW_FC и тех же голосах (критический провал), иначе 0.
    Результат клампится ±CRITICS_TIER_CLAMP (для одной ступени fc кламп
    ничего не меняет, но функция возвращает величину, готовую к суммированию
    со ступенью Tomatometer в apply_rt_to_score — суммарный кламп ±0.3).
    Без данных критиков (см. _get_critics_data) возвращается 0.0.

    Выделена из get_weighted_rating (B6): величина уже применённой ступени fc
    нужна пересчёту оценки после обогащения RT — из одной итоговой оценки s1
    ступень не извлекается.
    """
    critics = _get_critics_data(movie)
    if critics is None:
        return 0.0

    fc, fc_votes = critics
    tier = 0.0
    if fc_votes >= CRITICS_MIN_VOTES_TIER:
        if fc >= CRITICS_TIER_HIGH_FC:
            tier += CRITICS_TIER_BONUS
        elif fc <= CRITICS_TIER_LOW_FC:
            tier -= CRITICS_TIER_BONUS
    return max(-CRITICS_TIER_CLAMP, min(CRITICS_TIER_CLAMP, tier))


def get_weighted_rating(movie: Dict, is_russian_search: bool = False) -> float:
    """Взвешенный рейтинг фильма с учётом консенсуса кинокритиков (s1, A1).

    Формула (docs/research/research_rotten_tomatoes.md, §«Итоговая
    формула ранжирования»; числовые значения заданы константами модуля
    CRITICS_*/RATING_* — единая точка тюнинга):
    - base — базовая оценка КП/IMDb с прежними фолбэками; если base == 0.0
      (зрительских рейтингов нет вовсе), коррекция критиков не применяется:
      «нет зрительских данных» не должно порождать оценку из одних критиков;
    - шринк к консенсусу критиков при votes.filmCritics >=
      CRITICS_MIN_VOTES_SHRINK: s1 = base + CRITICS_SHRINK_FACTOR * (fc - base);
    - ступень fc — см. get_critics_tier (±CRITICS_TIER_BONUS при голосах >=
      CRITICS_MIN_VOTES_TIER, кламп ±CRITICS_TIER_CLAMP);
    - итог: score = clamp(s1 + tier, RATING_MIN, RATING_MAX).

    Без данных критиков (нет поля, fc == 0/None в любом представлении,
    votes < CRITICS_MIN_VOTES_SHRINK) возвращается base без изменений —
    полная обратная совместимость с прежней формулой.

    Возвращает оценку s1 — вход функции apply_rt_to_score (B6): учёт
    Tomatometer выполняется уже после обогащения финальной выдачи.
    """
    base = _get_base_weighted_rating(movie, is_russian_search)
    if base == 0.0:
        # Нет ни одного зрительского рейтинга: base — маркер «нет данных»,
        # шринк от нуля породил бы оценку из одних только критиков
        return base

    critics = _get_critics_data(movie)
    if critics is None:
        # Данных критиков нет: поведение ровно как до учёта критиков
        return base

    fc, _fc_votes = critics

    # Шринк к консенсусу: сдвигаем base на 20% разницы в сторону fc
    score = base + CRITICS_SHRINK_FACTOR * (fc - base)

    return max(RATING_MIN, min(RATING_MAX, score + get_critics_tier(movie)))


def apply_rt_to_score(
    score: float,
    rt_score: Optional[int],
    is_russian_search: bool = False,
    critics_tier: float = 0.0,
) -> float:
    """Пересчёт оценки ранжирования с учётом Tomatometer (фаза 1, Epic B, B6).

    Чистая функция: вход — оценка s1 после A1 (get_weighted_rating),
    `rt_score` (Tomatometer, процент 0–100 либо None), признак российской
    ветки и величина уже применённой ступени fc (get_critics_tier — нужна
    для суммарного клампа ступеней, т.к. из s1 ступень не извлекается).

    Формула (docs/research/research_rotten_tomatoes.md, §4 итоговая;
    константы RT_* — единая точка тюнинга):
    - нормализация: rt_norm = rt_score / RT_PERCENT_SCALE (процент → 0–10);
    - шринк: s2 = score + RT_SHRINK_FACTOR * (rt_norm − score);
    - ступень rt: +RT_TIER_BONUS при rt_score >= RT_TIER_HIGH,
      −RT_TIER_BONUS при rt_score <= RT_TIER_LOW, иначе 0;
    - суммарный кламп ступеней: tier_total = clamp(critics_tier + tier_rt,
      ±CRITICS_TIER_CLAMP); так как s1 уже включает ступень fc, итог:
      clamp(s2 − critics_tier + tier_total, RATING_MIN, RATING_MAX).

    Случаи неприменения (возврат score без изменений — обратная
    совместимость с фазой 0+A1+B5):
    - rt_score is None (нет IMDb ID, сбой обогащения, выключен флаг Epic B);
    - is_russian_search (российская ветка не затронута);
    - нечисловой rt_score/critics_tier (защита от мусорных данных);
    - score == 0.0 — маркер «нет зрительских рейтингов» (как в A1:
      отсутствие зрительских данных не порождает оценку из одних критиков).

    rt_score == 0 — валидная провальная оценка (0% «свежести»): шринк к 0.0
    и ступень −0.3 применяются (OMDb-пайплайн кодирует «нет данных» как None).
    """
    if is_russian_search or rt_score is None or score == 0.0:
        return score

    try:
        rt = float(rt_score)
        tier_fc = float(critics_tier)
    except (TypeError, ValueError):
        # Нечисловые данные трактуем как «нет оценок» — score без изменений
        return score

    # Шринк к консенсусу RT: нормализация процента в шкалу 0–10 обязательна
    rt_norm = rt / RT_PERCENT_SCALE
    shrunk = score + RT_SHRINK_FACTOR * (rt_norm - score)

    # Ступень Tomatometer «одобрено критиками RT» / «провал»
    tier_rt = 0.0
    if rt >= RT_TIER_HIGH:
        tier_rt = RT_TIER_BONUS
    elif rt <= RT_TIER_LOW:
        tier_rt = -RT_TIER_BONUS

    # Суммарный кламп ступеней fc + rt: ±0.3 на обе (не ±0.6)
    tier_total = max(-CRITICS_TIER_CLAMP, min(CRITICS_TIER_CLAMP, tier_fc + tier_rt))

    return max(RATING_MIN, min(RATING_MAX, shrunk - tier_fc + tier_total))


def rerank_with_rt(movies: List[Dict], is_russian_search: bool = False) -> List[Dict]:
    """Пересортировка финального списка (≤ limit) после обогащения RT (B6).

    Обогащение rt_score выполняется уже после фильтрации/ранжирования
    кандидатов (контракт B5 — только финал), поэтому RT учитывается здесь:
    каждому фильму пересчитывается `weighted_score` (s1 → s2, см.
    apply_rt_to_score), затем список стабильно сортируется по ключу
    `(country_priority, −weighted_score)` — тому же, что использовал
    filter_movies_by_quality с prioritize_english_speaking=True. Состав
    списка не меняется (RT-демотивация не отсеивает фильмы, прошедшие
    фильтр качества).

    No-op (порядок не трогается):
    - is_russian_search=True — российская ветка не затронута;
    - пустой список;
    - хотя бы у одного фильма `weighted_score` не число: словари без s1
      (чужие пути выдачи, например одиночные карточки) не пересортировываются.

    Обратная совместимость: если ни у одного фильма нет rt_score, все s2
    равны s1, а список уже отсортирован этим ключом — стабильная сортировка
    оставляет порядок идентичным фазе 0+A1+B5.
    """
    if is_russian_search or not movies:
        return movies

    for movie in movies:
        score = movie.get('weighted_score')
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            # Нет данных для пересчёта — порядок выдачи не трогаем
            return movies

    for movie in movies:
        critics_tier = movie.get('critics_tier')
        if isinstance(critics_tier, bool) or not isinstance(critics_tier, (int, float)):
            critics_tier = 0.0
        movie['weighted_score'] = apply_rt_to_score(
            float(movie['weighted_score']),
            movie.get('rt_score'),
            critics_tier=float(critics_tier),
        )

    movies.sort(
        key=lambda m: (m.get('country_priority', 0), -float(m.get('weighted_score', 0.0)))
    )
    return movies


def is_music_only_content(movie: Dict) -> bool:
    """Тайтл, у которого все жанры музыкальные (концерт/лайв/муз. документалка)."""
    genres = movie.get('genres', [])
    genre_names = {g.get('name', '').lower() for g in genres if isinstance(g, dict)}
    genre_names.discard('')
    return bool(genre_names) and genre_names <= MUSIC_ONLY_GENRES


def is_standup_content(movie: Dict) -> bool:
    """Стендап-выступление: жанры только комедийные + маркеры
    записанного выступления комика в названии/описании."""
    genres = movie.get('genres', [])
    genre_names = {g.get('name', '').lower() for g in genres if isinstance(g, dict)}
    genre_names.discard('')
    if not genre_names or not genre_names <= STANDUP_CONTENT_GENRES:
        return False

    text = ' '.join([
        str(movie.get('name') or ''),
        str(movie.get('description') or ''),
    ]).lower()

    if any(marker in text for marker in STANDUP_EXPLICIT_MARKERS):
        return True

    has_performance = any(marker in text for marker in STANDUP_PERFORMANCE_MARKERS)
    has_companion = any(marker in text for marker in STANDUP_COMPANION_MARKERS)
    return has_performance and has_companion


def should_exclude_by_genre(movie: Dict, allowed_excluded_genres: Optional[Set[str]] = None) -> bool:
    if allowed_excluded_genres is None:
        allowed_excluded_genres = set()

    genres = movie.get('genres', [])
    genre_names = {g.get('name', '').lower() for g in genres if isinstance(g, dict)}
    genre_names.discard('')

    allowed_match = genre_names & allowed_excluded_genres

    found_excluded = genre_names & EXCLUDED_GENRES
    if found_excluded and not allowed_match:
        return True

    if is_music_only_content(movie) and not allowed_match:
        return True

    if is_standup_content(movie) and 'стендап' not in allowed_excluded_genres:
        return True

    return False


def filter_movies_by_quality(
        movies: List[Dict],
        year: Optional[int] = None,
        min_rating: float = 6.0,
        min_votes_override: Optional[int] = None,
        exclude_anime: bool = True,
        prioritize_english_speaking: bool = False,
        allowed_excluded_genres: Optional[Set[str]] = None,
        is_russian_search: bool = False
) -> List[Dict]:
    if allowed_excluded_genres is None:
        allowed_excluded_genres = set()

    def _calculate_min_votes(y: Optional[int]) -> int:
        if not y:
            return 1000
        if y >= 2020:
            return 100
        elif y >= 2010:
            return 500
        elif y >= 2000:
            return 1000
        else:
            return 5000

    min_votes = min_votes_override if min_votes_override is not None else _calculate_min_votes(year)
    filtered = []

    for movie in movies:
        if should_exclude_by_genre(movie, allowed_excluded_genres):
            name = movie.get('name', 'Unknown')
            logger.debug(f"[MovieFilter] Пропущен по жанру: {name}")
            continue

        if exclude_anime:
            genres = movie.get('genres', [])
            genre_names = {g.get('name', '').lower() for g in genres if isinstance(g, dict)}
            if 'аниме' in genre_names and 'аниме' not in allowed_excluded_genres:
                name = movie.get('name', 'Unknown')
                logger.debug(f"[MovieFilter] Пропущен аниме: {name}")
                continue

        votes = movie.get('votes', {})
        imdb_votes = votes.get('imdb') or 0
        kp_votes = votes.get('kp') or 0

        weighted_rating = get_weighted_rating(movie, is_russian_search)
        best_votes = max(imdb_votes, kp_votes)

        if weighted_rating < min_rating:
            continue
        if best_votes < min_votes:
            continue

        movie['weighted_rating'] = weighted_rating
        filtered.append(movie)

    if prioritize_english_speaking:
        def _sort_key(m):
            return (get_country_priority(m), -m.get('weighted_rating', 0))

        filtered.sort(key=_sort_key)
    else:
        def _sort_key(m):
            return -m.get('weighted_rating', 0)

        filtered.sort(key=_sort_key)

    for movie in filtered:
        if 'weighted_rating' in movie:
            del movie['weighted_rating']

    logger.info(f"[MovieFilter] Отфильтровано: {len(filtered)} фильмов из {len(movies)}")
    return filtered
