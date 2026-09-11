# src/rt_enrichment.py
"""Пайплайн обогащения финальной выдачи оценками Rotten Tomatoes (Epic B, B5).

Единственная точка сборки блоков B1–B4: feature flag
(`config.rt_scores_enabled`), клиент OMDb (`omdb_client.OmdbClient`),
join-ключ (поле `imdb_id` итогового словаря фильма, B3) и кэш оценок
(`rt_cache.aget_scores`/`aset_scores`, B4).

Контракты (зафиксированы дельта-спекой изменения add-rt-enrichment):

- обогащается ТОЛЬКО финальная выдача — список после
  `filter_movies_by_quality` и обрезки до `limit` (≤ 13 на практике);
  кандидаты (до 250 записей) не трогаются вовсе;
- порядок на один фильм: кэш → при промахе OMDb → запись результата в кэш.
  В кэш передаётся результат источника целиком: `None` (сбой) — no-op,
  `RtScores(None, None)` — отрицательный кэш; hit и отрицательный hit в
  OMDb не идут;
- параллельность: `asyncio.gather` + `asyncio.Semaphore`
  (`RT_ENRICH_CONCURRENCY`, дефолт 5 — согласовано с `pool_size` пула кэша);
- общий дедлайн: `asyncio.wait_for` (`RT_ENRICH_DEADLINE_SECONDS`, дефолт 4 с).
  Дедлайн обязателен: при недоступном PostgreSQL блокирующее чтение кэша
  живёт до TCP-таймаута psycopg2, и fail-silent кэша спасает от исключения,
  но не от задержки выдачи;
- fail-silent: любой сбой (OMDb, кэш, БД, дедлайн, неожиданное исключение) —
  выдача возвращается целиком, исключение наружу не пробрасывается; оценки,
  полученные до дедлайна, сохраняются (лучше часть бейджей, чем ни одного);
- поля `rt_score`/`metascore` присутствуют в словаре фильма ВСЕГДА (в том
  числе при выключенном флаге и при сбое) — структура словаря стабильна для
  B6 (ранжирование) и B7 (бейдж RT в списках и карточках);
- словари дополняются на месте, функция возвращает тот же список: именно эти
  объекты хранит in-memory кэш `MovieAgent._search_cache`, поэтому повторная
  выдача из кэша не платит ни за OMDb, ни за латентность.
"""
import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp

import config
from config import rt_scores_enabled
from omdb_client import OmdbClient, RtScores
from rt_cache import aget_scores, aset_scores

logger = logging.getLogger(__name__)

# Результат обращения по одному IMDb ID: оценки (либо None) и признак того,
# что за ними пришлось идти к источнику (для лога расхода квоты OMDb).
IdOutcome = Tuple[Optional[RtScores], bool]

# Лимит одновременных обращений по умолчанию: верхняя граница диапазона 3–5
# из задачи B5. Согласован с ENGINE_OPTIONS кэша (pool_size=5): каждый
# параллельный фильм может держать одно соединение к PostgreSQL, поэтому
# пул не уходит в max_overflow.
DEFAULT_CONCURRENCY = 5

# Общий дедлайн обогащения одной выдачи (секунды) по умолчанию. Меньше
# таймаута одного запроса OMDb (8 с), поэтому «висящий» запрос отсекается,
# а не дожидается; достаточно для 13–30 запросов при типичной латентности
# 100–300 мс. Жёсткий потолок латентности выдачи.
DEFAULT_DEADLINE_SECONDS = 4.0

# Предохранитель дневной квоты OMDb (free: 1000 запросов/день): за один вызов
# обогащается не более этого числа уникальных IMDb ID. В существующих путях
# вызова не срабатывает (максимальный limit выдачи в dialogue_manager — 30),
# защищает от будущего вызова с большим limit (например, 250).
MAX_MOVIES_PER_CALL = 30


def _concurrency_limit() -> int:
    """Лимит одновременности из RT_ENRICH_CONCURRENCY (читается в момент вызова).

    Валидация по образцу rt_cache._ttl_days(): нечисло или значение меньше 1
    игнорируются в пользу дефолта. Верхнего клампинга нет — эффективный
    потолок всё равно равен длине финального списка (не более
    MAX_MOVIES_PER_CALL).
    """
    raw = os.getenv('RT_ENRICH_CONCURRENCY')
    if raw is None:
        return DEFAULT_CONCURRENCY
    try:
        value = int(raw.strip())
    except ValueError:
        logger.debug(f"RT_ENRICH_CONCURRENCY={raw!r} не является числом — использую дефолт {DEFAULT_CONCURRENCY}")
        return DEFAULT_CONCURRENCY
    if value < 1:
        logger.debug(f"RT_ENRICH_CONCURRENCY={value} меньше 1 — использую дефолт {DEFAULT_CONCURRENCY}")
        return DEFAULT_CONCURRENCY
    return value


def _deadline_seconds() -> float:
    """Общий дедлайн из RT_ENRICH_DEADLINE_SECONDS (читается в момент вызова).

    Нечисло, ноль и отрицательное значение игнорируются в пользу дефолта:
    дедлайн обязан оставаться положительным, иначе wait_for отменил бы
    обогащение мгновенно и все выдачи были бы без оценок.
    """
    raw = os.getenv('RT_ENRICH_DEADLINE_SECONDS')
    if raw is None:
        return DEFAULT_DEADLINE_SECONDS
    try:
        value = float(raw.strip())
    except ValueError:
        logger.debug(f"RT_ENRICH_DEADLINE_SECONDS={raw!r} не является числом — использую дефолт {DEFAULT_DEADLINE_SECONDS}")
        return DEFAULT_DEADLINE_SECONDS
    if value <= 0:
        logger.debug(f"RT_ENRICH_DEADLINE_SECONDS={value} неположительный — использую дефолт {DEFAULT_DEADLINE_SECONDS}")
        return DEFAULT_DEADLINE_SECONDS
    return value


def _make_client() -> OmdbClient:
    """Клиент OMDb: один экземпляр на вызов обогащения (не на фильм).

    Ключ берётся из атрибута модуля config в момент вызова (обращение
    `config.OMDB_API_KEY`, а не `from config import OMDB_API_KEY`, позволяет
    тестам подменять ключ через monkeypatch.setattr). Ленивый синглтон модуля
    намеренно не используется: клиент хранит ключ, а флаг Epic B читает env в
    момент вызова — синглтон законсервировал бы устаревший ключ.
    Создание клиента — два присваивания, стоимости нет. Ключ не логируется.
    """
    return OmdbClient(config.OMDB_API_KEY)


def _ensure_score_fields(movies: List[Dict[str, Any]]) -> None:
    """Проставить поля rt_score/metascore всем словарям списка (по умолчанию None).

    Структура итогового словаря одинакова при любом исходе обогащения —
    потребители (B6/B7) не различают «поля нет» и «поле None». Используется
    setdefault, а не присваивание: повторный вызов на уже обогащённом списке
    (например, доставленном из кэша MovieAgent) не затирает имеющиеся оценки
    до получения новых.
    """
    for movie in movies:
        if isinstance(movie, dict):
            movie.setdefault('rt_score', None)
            movie.setdefault('metascore', None)


def _collect_targets(movies: List[Dict[str, Any]]) -> List[str]:
    """Уникальные непустые IMDb ID финального списка в порядке появления.

    Фильмы без IMDb ID (по данным B3 — около 31% базы) пропускаются до любых
    обращений: ни кэша, ни OMDb. Дедупликация экономит квоту и исключает гонку
    «два параллельных запроса одного фильма до записи в кэш». Срез
    MAX_MOVIES_PER_CALL — предохранитель дневной квоты OMDb.
    """
    unique: List[str] = []
    seen: Set[str] = set()
    for movie in movies:
        if not isinstance(movie, dict):
            continue
        imdb_id = movie.get('imdb_id')
        if not isinstance(imdb_id, str):
            continue
        key = imdb_id.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(key)
    if len(unique) > MAX_MOVIES_PER_CALL:
        logger.debug(
            f"[RT] Финальный список длиннее предохранителя квоты: "
            f"обогащаются первые {MAX_MOVIES_PER_CALL} IMDb ID из {len(unique)}"
        )
        return unique[:MAX_MOVIES_PER_CALL]
    return unique


async def _fetch_scores_for_id(
    session: aiohttp.ClientSession,
    client: OmdbClient,
    semaphore: asyncio.Semaphore,
    imdb_id: str,
    outcomes: Dict[str, IdOutcome],
) -> None:
    """Обращение по одному IMDb ID: кэш → при промахе OMDb → запись в кэш.

    Результат складывается в общий словарь `outcomes` сразу по получении —
    поэтому оценки, найденные до срабатывания общего дедлайна, не теряются
    при отмене остальных обращений.

    Семафор покрывает весь конвейер фильма, а не только HTTP-запрос:
    одновременно ограничены и потоки asyncio.to_thread к PostgreSQL, их число
    не превышает pool_size пула кэша.
    """
    async with semaphore:
        try:
            lookup = await aget_scores(imdb_id)
            if lookup.hit:
                # Попадание в кэш, включая отрицательное (is_negative:
                # RtScores(None, None)) — повторно в OMDb не идём
                outcomes[imdb_id] = (lookup.scores, False)
                return
            scores = await client.fetch_rt_scores(session, imdb_id)
            # В кэш передаётся результат источника целиком: None (сбой или
            # выключенный флаг) — no-op, сбой не превращается в отрицательный
            # кэш на весь TTL
            await aset_scores(imdb_id, scores)
            outcomes[imdb_id] = (scores, True)
        except Exception as exc:
            # Сбой одного фильма не влияет на остальные (fail-silent). В лог
            # пишется только имя типа исключения: str() может содержать DSN
            # PostgreSQL с паролем или URL запроса с apikey.
            logger.warning(f"[RT] Не удалось получить оценки для {imdb_id}: {type(exc).__name__}")
            outcomes[imdb_id] = (None, False)


async def _enrich_all(
    session: aiohttp.ClientSession,
    client: OmdbClient,
    imdb_ids: List[str],
    outcomes: Dict[str, IdOutcome],
) -> None:
    """Параллельные обращения по всем IMDb ID (результаты — в `outcomes`).

    return_exceptions=True — второй уровень fail-silent: неожиданное
    исключение одного элемента приходит значением и не отменяет соседние
    (первый уровень — try/except внутри _fetch_scores_for_id). Исключений
    в штатном режиме здесь не бывает, ветка нужна как страховка от бага
    обвязки: она логируется и трактуется как «оценок нет».
    """
    semaphore = asyncio.Semaphore(_concurrency_limit())
    gathered = await asyncio.gather(
        *(_fetch_scores_for_id(session, client, semaphore, imdb_id, outcomes) for imdb_id in imdb_ids),
        return_exceptions=True,
    )
    for imdb_id, outcome in zip(imdb_ids, gathered):
        if isinstance(outcome, BaseException):
            logger.warning(f"[RT] Сбой обогащения для {imdb_id}: {type(outcome).__name__}")
            outcomes.setdefault(imdb_id, (None, False))


def _apply_scores(movies: List[Dict[str, Any]], outcomes: Dict[str, IdOutcome]) -> int:
    """Перенести полученные оценки в словари фильмов; вернуть число обогащённых.

    Обогащённым считается фильм, получивший хотя бы одну оценку: пустой
    результат источника (RtScores(None, None)), сбой и фильмы вне
    предохранителя квоты остаются с полями None, проставленными
    _ensure_score_fields. Сопоставление идёт по IMDb ID через словарь,
    поэтому порядок фильмов и соответствие оценок не зависят от порядка
    завершения параллельных обращений.
    """
    enriched = 0
    for movie in movies:
        if not isinstance(movie, dict):
            continue
        imdb_id = movie.get('imdb_id')
        if not isinstance(imdb_id, str):
            continue
        outcome = outcomes.get(imdb_id.strip())
        if outcome is None:
            continue
        scores = outcome[0]
        if scores is None:
            continue
        movie['rt_score'] = scores.rt_score
        movie['metascore'] = scores.metascore
        if scores.rt_score is not None or scores.metascore is not None:
            enriched += 1
    return enriched


async def enrich_movies_with_rt_scores(
    session: aiohttp.ClientSession,
    movies: List[Dict[str, Any]],
    client: Optional[OmdbClient] = None,
) -> List[Dict[str, Any]]:
    """Обогатить финальную выдачу оценками Rotten Tomatoes/Metacritic.

    Дополняет словари фильмов полями `rt_score` (Tomatometer, 0–100) и
    `metascore` (0–100) — либо None — и возвращает тот же список (дополнение
    на месте, см. docstring модуля). Параметр `client` — seam для тестов и
    для переиспользования клиента; по умолчанию создаётся один `OmdbClient`
    на вызов, запросы идут через переданную вызывающим кодом HTTP-сессию.

    Fail-silent: любой сбой (OMDb, кэш, БД, превышение общего дедлайна,
    неожиданное исключение) приводит к выдаче без проброса исключения
    вызывающему коду. При выключенном feature flag Epic B функция является
    быстрым no-op: поля проставляются None, обращений к источнику и к
    хранилищу кэша нет, клиент не создаётся.
    """
    if not movies:
        return movies

    _ensure_score_fields(movies)

    if not rt_scores_enabled():
        # Флаг Epic B выключен: не тратим ни запросы к OMDb, ни обращения к БД
        logger.debug('[RT] RT-оценки выключены feature flag — обогащение пропущено')
        return movies

    imdb_ids = _collect_targets(movies)
    if not imdb_ids:
        logger.debug('[RT] В финальной выдаче нет фильмов с IMDb ID — обогащение пропущено')
        return movies

    outcomes: Dict[str, IdOutcome] = {}
    try:
        # Создание клиента — внутри try (фикс minor ревью B5, design.md D7
        # add-rt-enrichment): сбой _make_client тоже поглощается fail-silent
        # третьего уровня — выдача возвращается с rt_score=None.
        omdb = client if client is not None else _make_client()
        await asyncio.wait_for(
            _enrich_all(session, omdb, imdb_ids, outcomes),
            timeout=_deadline_seconds(),
        )
    except Exception as exc:
        # Превышение общего дедлайна (asyncio.TimeoutError в Python 3.10 —
        # concurrent.futures.TimeoutError, в 3.11 — встроенный TimeoutError)
        # либо сбой обвязки: выдача возвращается немедленно. CancelledError
        # (BaseException) намеренно не перехватывается — отмена задачи бота
        # должна проходить насквозь. Оценки, полученные до дедлайна, уже лежат
        # в outcomes и будут применены ниже (design.md D6).
        logger.warning(
            f"[RT] Обогащение оценок не завершено ({type(exc).__name__}) — "
            f"выдача возвращается без недостающих оценок"
        )

    enriched = _apply_scores(movies, outcomes)
    source_calls = sum(1 for outcome in outcomes.values() if outcome[1])
    logger.debug(
        f"[RT] Обогащено {enriched} из {len(movies)} фильмов "
        f"(с IMDb ID: {len(imdb_ids)}, обращений к источнику: {source_calls})"
    )
    return movies
