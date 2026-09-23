# tests/test_rt_enrichment.py
"""Тесты B5: пайплайн обогащения финальной выдачи RT-оценками.

Без сети и без живой БД: OMDb-клиент и кэш оценок — фейковые
(monkeypatch.setattr по модулю rt_enrichment), сессия — sentinel-объект.
Проверяются контракт дельта-спеки add-rt-enrichment (R1–R8) и design.md:

- обогащается только финал (сквозные тесты на движке/агенте/боте с 40
  кандидатами и limit=13), дедупликация и предохранитель квоты (D8, D9);
- кэш-первый порядок: hit и negative hit не идут в источник, miss пишет
  результат источника в кэш целиком (None — no-op, RtScores(None, None) —
  отрицательный кэш) (D7, R3);
- семафор одновременности и параллелизм (D4, R4);
- общий дедлайн, в том числе отсечение блокирующего потока к «недоступной
  БД» (asyncio.to_thread + time.sleep) и сохранение частичного результата
  (D5, D6, R5);
- fail-silent на всех уровнях: сбой одного фильма, кэша, источника,
  дедлайн — выдача цела, исключение не пробрасывается (D7, R6);
- feature flag: выключен (или включён без ключа) — ноль обращений к
  источнику и кэшу, клиент не создаётся, поля None (R7);
- ресурсы: один клиент на вызов, сессия вызывающего кода переиспользуется,
  API-ключ не попадает в логи (D10, D11, R2, R8).

conftest.py уже добавляет src/ в sys.path. Флаг Epic B задаётся через
monkeypatch.setenv — config.rt_scores_enabled читает env в момент вызова
(свойство B1); локальный .env может содержать реальный OMDB_API_KEY,
поэтому фикстуры выставляют переменные явно.
"""
import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import aiohttp
import pytest

import config
import rt_cache
import rt_enrichment
import telegram_bot
from movie_agent import MovieAgent
from omdb_client import RtScores
from recommendation_engine import RecommendationEngine
from rt_cache import CacheLookup

# Тестовые оценки по умолчанию для фейкового источника
DEFAULT_SCORES = RtScores(rt_score=91, metascore=82)


# --- Фикстуры и фейки (задача 4.1) ---


@pytest.fixture(autouse=True)
def reset_env(monkeypatch):
    """Сброс env-настроек пайплайна: тесты не зависят от локального .env."""
    monkeypatch.delenv('RT_ENRICH_CONCURRENCY', raising=False)
    monkeypatch.delenv('RT_ENRICH_DEADLINE_SECONDS', raising=False)


@pytest.fixture()
def flag_on(monkeypatch):
    """Feature flag Epic B включён; ключ — атрибут модуля config (D10)."""
    monkeypatch.setenv('ENABLE_RT_SCORES', 'true')
    monkeypatch.setenv('OMDB_API_KEY', 'test-omdb-key')
    monkeypatch.setattr(config, 'OMDB_API_KEY', 'test-omdb-key')


@pytest.fixture()
def flag_off(monkeypatch):
    """Feature flag Epic B гарантированно выключен (R7)."""
    monkeypatch.setenv('ENABLE_RT_SCORES', 'false')


class _FakeOmdbClient:
    """Фейковый клиент OMDb: счётчик вызовов, пик одновременности, результаты.

    results: imdb_id -> RtScores | None | Exception (исключение пробрасывается
    из fetch — проверка per-item fail-silent); default — результат для id,
    которых нет в results; delay — задержка обращения (секунды).
    """

    def __init__(
        self,
        results: Optional[Dict[str, Any]] = None,
        default: Any = DEFAULT_SCORES,
        delay: float = 0.0,
    ):
        self.results: Dict[str, Any] = dict(results or {})
        self.default = default
        self.delay = delay
        self.calls: List[str] = []
        self.sessions: List[Any] = []
        self._current = 0
        self.peak = 0

    async def fetch_rt_scores(self, session: Any, imdb_id: str) -> Optional[RtScores]:
        self.calls.append(imdb_id)
        self.sessions.append(session)
        self._current += 1
        self.peak = max(self.peak, self._current)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            outcome = self.results.get(imdb_id, self.default)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            self._current -= 1


class _FakeCache:
    """Фейковые aget_scores/aset_scores: история вызовов и заданные попадания.

    get_raises: imdb_id -> исключение при чтении (имитация сбоя БД для
    конкретного фильма или для всех, если задано в set_raises/get_raises).
    """

    def __init__(
        self,
        hits: Optional[Dict[str, CacheLookup]] = None,
        get_raises: Optional[Dict[str, Exception]] = None,
        set_raises: Optional[Dict[str, Exception]] = None,
    ):
        self.hits: Dict[str, CacheLookup] = dict(hits or {})
        self.get_raises: Dict[str, Exception] = dict(get_raises or {})
        self.set_raises: Dict[str, Exception] = dict(set_raises or {})
        self.get_calls: List[str] = []
        self.set_calls: List[Any] = []

    async def aget_scores(self, imdb_id: Optional[str], app: Any = None) -> CacheLookup:
        self.get_calls.append(imdb_id)
        exc = self.get_raises.get(imdb_id or '')
        if exc is not None:
            raise exc
        return self.hits.get(imdb_id or '', rt_cache.MISS)

    async def aset_scores(self, imdb_id: Optional[str], scores: Optional[RtScores], app: Any = None) -> None:
        self.set_calls.append((imdb_id, scores))
        exc = self.set_raises.get(imdb_id or '')
        if exc is not None:
            raise exc


@pytest.fixture()
def fake_cache(monkeypatch) -> _FakeCache:
    """Подменить кэш B4 фейковым (без живой БД), вернуть объект с историей."""
    cache = _FakeCache()
    monkeypatch.setattr(rt_enrichment, 'aget_scores', cache.aget_scores)
    monkeypatch.setattr(rt_enrichment, 'aset_scores', cache.aset_scores)
    return cache


@pytest.fixture()
def sentinel_cache(monkeypatch) -> List[Any]:
    """Кэш-сенсоры: фиксируют след и падают (по образцу tests/test_rt_cache.py).

    AssertionError проглатывается контуром fail-silent пайплайна, поэтому
    доказательством отсутствия обращений служит пустой список calls.
    """
    calls: List[Any] = []

    async def _aget(imdb_id: Optional[str], app: Any = None) -> CacheLookup:
        calls.append(('get', imdb_id))
        raise AssertionError('Обращение к кэшу запрещено в этом тесте')

    async def _aset(imdb_id: Optional[str], scores: Optional[RtScores], app: Any = None) -> None:
        calls.append(('set', imdb_id))
        raise AssertionError('Обращение к кэшу запрещено в этом тесте')

    monkeypatch.setattr(rt_enrichment, 'aget_scores', _aget)
    monkeypatch.setattr(rt_enrichment, 'aset_scores', _aset)
    return calls


def _install_sentinel_client(monkeypatch) -> List[str]:
    """Сенсор создания клиента OMDb: след фиксируется ДО raise (D10, R7)."""
    created: List[str] = []

    def _boom():
        created.append('client')
        raise AssertionError('Клиент OMDb не должен создаваться в этом тесте')

    monkeypatch.setattr(rt_enrichment, '_make_client', _boom)
    return created


def _install_fake_client(monkeypatch, client: _FakeOmdbClient) -> None:
    """Пайплайн без явного client= берёт его из _make_client — подменяем."""
    monkeypatch.setattr(rt_enrichment, '_make_client', lambda: client)


def _movie(imdb_id: Optional[str], title: str = 'Фильм', **extra: Any) -> Dict[str, Any]:
    """Итоговый словарь фильма в формате _format_movies_list (B3, R2)."""
    data: Dict[str, Any] = {
        'id': 326,
        'title': title,
        'year': 1994,
        'genre': 'драма',
        'country': 'США',
        'rating': 7.5,
        'rating_imdb': 7.5,
        'rating_kp': 8.1,
        'rating_source': 'IMDB',
        'critics_rating': None,
        'critics_votes': None,
        'imdb_id': imdb_id,
        'description': 'Описание',
        'poster_url': 'https://example.com/poster.jpg',
        'kinopoisk_url': 'https://www.kinopoisk.ru/film/326/',
        'type': 'movie',
    }
    data.update(extra)
    return data


class _StubKinopoiskClient:
    """Заглушка клиента Кинопоиска по образцу tests/test_recommendation_engine.py."""

    def __init__(self, docs: List[Dict[str, Any]]):
        self.docs = docs
        self.search_calls: List[Dict[str, Any]] = []
        self.top_calls: List[Dict[str, Any]] = []

    async def search_movies(self, session: Any, **kwargs: Any) -> Dict[str, Any]:
        self.search_calls.append(kwargs)
        return {'docs': list(self.docs)}

    async def search_recommendation(self, session: Any, **kwargs: Any) -> Dict[str, Any]:
        self.top_calls.append(kwargs)
        return {'docs': []}


def _kp_doc(doc_id: int, imdb_id: Optional[str]) -> Dict[str, Any]:
    """Сырой документ kinopoisk.dev, проходящий filter_movies_by_quality."""
    return {
        'id': doc_id,
        'name': f'Фильм {doc_id}',
        'year': 2019,
        'genres': [{'name': 'драма'}],
        'countries': [{'name': 'США'}],
        'rating': {'imdb': 7.5, 'kp': None, 'filmCritics': None},
        'votes': {'imdb': 100000, 'kp': 0, 'filmCritics': None},
        'externalId': {'imdb': imdb_id},
        'type': 'movie',
    }


class _FakeHttpResponse:
    """Ответ фейковой HTTP-сессии: один и тот же JSON на любой GET."""

    def __init__(self, payload: Any, status: int = 200):
        self.status = status
        self._payload = payload

    async def json(self) -> Any:
        return self._payload

    async def __aenter__(self) -> '_FakeHttpResponse':
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


class _FakeHttpSession:
    """Фейковая aiohttp-сессия для хендлера бота (без сети)."""

    def __init__(self, payload: Any, status: int = 200):
        self.payload = payload
        self.status = status
        self.get_calls: List[Any] = []

    def get(self, url: str, **kwargs: Any) -> _FakeHttpResponse:
        self.get_calls.append((url, kwargs))
        return _FakeHttpResponse(self.payload, self.status)

    async def __aenter__(self) -> '_FakeHttpSession':
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


# --- 1.2: env-хелперы лимита одновременности и дедлайна ---


def test_concurrency_from_env(monkeypatch):
    """RT_ENRICH_CONCURRENCY читается в момент вызова."""
    monkeypatch.setenv('RT_ENRICH_CONCURRENCY', '3')
    assert rt_enrichment._concurrency_limit() == 3


@pytest.mark.parametrize('raw_value', ['abc', '', '0', '-2', '2.5'])
def test_concurrency_invalid_falls_back_to_default(monkeypatch, raw_value):
    """Некорректный лимит (нечисло, < 1) —> дефолт 5, без исключений."""
    monkeypatch.setenv('RT_ENRICH_CONCURRENCY', raw_value)
    assert rt_enrichment._concurrency_limit() == rt_enrichment.DEFAULT_CONCURRENCY == 5


def test_deadline_from_env(monkeypatch):
    monkeypatch.setenv('RT_ENRICH_DEADLINE_SECONDS', '2.5')
    assert rt_enrichment._deadline_seconds() == 2.5


@pytest.mark.parametrize('raw_value', ['abc', '', '0', '-1'])
def test_deadline_invalid_falls_back_to_default(monkeypatch, raw_value):
    """Некорректный дедлайн (нечисло, <= 0) —> дефолт 4.0, без исключений."""
    monkeypatch.setenv('RT_ENRICH_DEADLINE_SECONDS', raw_value)
    assert rt_enrichment._deadline_seconds() == rt_enrichment.DEFAULT_DEADLINE_SECONDS == 4.0


# --- 1.3 / R2: поля оценок и регрессия существующих полей ---


def test_fields_present_when_flag_off(flag_off, sentinel_cache, monkeypatch):
    """Флаг выключен — поля rt_score/metascore всё равно присутствуют (None)."""
    created = _install_sentinel_client(monkeypatch)
    movies = [_movie('tt0111161')]
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies))
    assert result is movies
    assert 'rt_score' in result[0] and 'metascore' in result[0]
    assert result[0]['rt_score'] is None
    assert result[0]['metascore'] is None
    assert created == []
    assert sentinel_cache == []


def test_existing_fields_untouched(flag_on, fake_cache):
    """Регрессия (R2): состав и значения существующих полей не изменились."""
    movie = _movie('tt0111161', title='Побег из Шоушенка')
    before = dict(movie)
    fake_cache.hits['tt0111161'] = CacheLookup(hit=True, scores=DEFAULT_SCORES)
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(
        None, [movie], client=_FakeOmdbClient()
    ))
    after = result[0]
    for key, value in before.items():
        assert after[key] == value
    assert set(after) == set(before) | {'rt_score', 'metascore'}
    assert after['rt_score'] == 91
    assert after['metascore'] == 82


def test_api_key_not_logged(flag_on, fake_cache, monkeypatch, caplog):
    """R8: API-ключ не попадает в логи — ни в rt_enrichment, ни в OmdbClient.

    Используется реальный OmdbClient и сессия, бросающая исключение с текстом
    ключа (str(exc) у aiohttp содержит URL с apikey=). Клиент B2 маскирует
    ключ, пайплайн B5 логирует только имя типа исключения.
    """
    secret = 'super-secret-key-123'
    monkeypatch.setenv('OMDB_API_KEY', secret)
    monkeypatch.setattr(config, 'OMDB_API_KEY', secret)

    class _BoomSession:
        def get(self, url: str, **kwargs: Any):
            raise RuntimeError(f'соединение разорвано: {url}?apikey={secret}')

    movies = [_movie('tt0111161')]
    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(_BoomSession(), movies))
    assert result[0]['rt_score'] is None
    assert secret not in caplog.text
    # OmdbClient замаскировал ключ в своём сообщении об ошибке
    assert '***' in caplog.text


# --- 1.4 / R1, R3: сбор целей — дедупликация, пропуск без IMDb ID, квота ---


def test_movie_without_imdb_id_is_skipped(flag_on, fake_cache):
    """Фильм без IMDb ID: ни кэша, ни источника; остаётся в выдаче с None."""
    omdb = _FakeOmdbClient()
    movies = [_movie(None, title='Без ID'), _movie('tt0111161', title='С ID')]
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies, client=omdb))
    assert fake_cache.get_calls == ['tt0111161']
    assert omdb.calls == ['tt0111161']
    # Порядок выдачи не изменился (R2)
    assert [m['title'] for m in result] == ['Без ID', 'С ID']
    assert result[0]['rt_score'] is None and result[0]['metascore'] is None
    assert result[1]['rt_score'] == 91 and result[1]['metascore'] == 82


def test_duplicate_imdb_id_fetched_once(flag_on, fake_cache):
    """Дубликат IMDb ID — одно обращение, оценки у обоих фильмов (D8)."""
    omdb = _FakeOmdbClient()
    movies = [_movie('tt0111161', title='Первый'), _movie('tt0111161', title='Дубликат')]
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies, client=omdb))
    assert omdb.calls == ['tt0111161']
    assert len(fake_cache.set_calls) == 1
    assert result[0]['rt_score'] == result[1]['rt_score'] == 91
    assert result[0]['metascore'] == result[1]['metascore'] == 82


def test_max_movies_per_call_caps_enrichment(flag_on, fake_cache):
    """Предохранитель квоты (D9): 40 уникальных ID — обогащаются первые 30."""
    omdb = _FakeOmdbClient()
    movies = [_movie(f'tt{i:07d}', title=f'Фильм {i}') for i in range(1, 41)]
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies, client=omdb))
    assert len(result) == 40
    assert len(omdb.calls) == rt_enrichment.MAX_MOVIES_PER_CALL == 30
    assert omdb.calls == [f'tt{i:07d}' for i in range(1, 31)]
    enriched = [m for m in result if m['rt_score'] is not None]
    assert len(enriched) == 30
    assert all(m['rt_score'] is None and m['metascore'] is None for m in result[30:])


# --- 1.5 / R3: кэш-первый порядок ---


def test_cache_hit_skips_omdb(flag_on, fake_cache):
    """Попадание в кэш — источник не вызывается, оценки из кэша."""
    fake_cache.hits['tt0111161'] = CacheLookup(hit=True, scores=RtScores(rt_score=91, metascore=82))
    omdb = _FakeOmdbClient()
    movies = [_movie('tt0111161')]
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies, client=omdb))
    assert omdb.calls == []
    assert fake_cache.set_calls == []
    assert result[0]['rt_score'] == 91
    assert result[0]['metascore'] == 82


def test_negative_cache_hit_skips_omdb(flag_on, fake_cache):
    """Отрицательный кэш (RtScores(None, None)) — hit: в OMDb не идём, поля None."""
    fake_cache.hits['tt9999999'] = CacheLookup(hit=True, scores=RtScores(rt_score=None, metascore=None))
    omdb = _FakeOmdbClient()
    movies = [_movie('tt9999999')]
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies, client=omdb))
    assert omdb.calls == []
    assert fake_cache.set_calls == []
    assert result[0]['rt_score'] is None
    assert result[0]['metascore'] is None


def test_cache_miss_fetches_and_writes(flag_on, fake_cache):
    """Промах — источник вызван, его результат записан в кэш целиком."""
    omdb = _FakeOmdbClient(results={'tt0111161': RtScores(rt_score=77, metascore=66)})
    movies = [_movie('tt0111161')]
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies, client=omdb))
    assert omdb.calls == ['tt0111161']
    assert fake_cache.set_calls == [('tt0111161', RtScores(rt_score=77, metascore=66))]
    assert result[0]['rt_score'] == 77
    assert result[0]['metascore'] == 66


def test_omdb_failure_is_not_cached(flag_on, fake_cache):
    """Сбой источника (None) — aset_scores получает None: реальный кэш — no-op.

    Сбой не превращается в отрицательный кэш на весь TTL (контракт B4).
    """
    omdb = _FakeOmdbClient(results={'tt0111161': None})
    movies = [_movie('tt0111161')]
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies, client=omdb))
    assert omdb.calls == ['tt0111161']
    assert fake_cache.set_calls == [('tt0111161', None)]
    assert result[0]['rt_score'] is None
    assert result[0]['metascore'] is None


def test_source_without_scores_is_cached_negative(flag_on, fake_cache):
    """Источник ответил без оценок — в кэш пишется RtScores(None, None)."""
    omdb = _FakeOmdbClient(results={'tt0111161': RtScores(rt_score=None, metascore=None)})
    movies = [_movie('tt0111161')]
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies, client=omdb))
    assert fake_cache.set_calls == [('tt0111161', RtScores(rt_score=None, metascore=None))]
    assert result[0]['rt_score'] is None
    assert result[0]['metascore'] is None


# --- R4: параллельность и семафор ---


def test_semaphore_limits_peak_concurrency(flag_on, fake_cache):
    """Пик одновременности не превышает дефолтный лимит 5 (D4)."""
    omdb = _FakeOmdbClient(delay=0.05)
    movies = [_movie(f'tt{i:07d}') for i in range(1, 14)]
    asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies, client=omdb))
    assert len(omdb.calls) == 13
    assert 2 <= omdb.peak <= rt_enrichment.DEFAULT_CONCURRENCY


def test_semaphore_limit_from_env(flag_on, fake_cache, monkeypatch):
    """RT_ENRICH_CONCURRENCY=3 — пик одновременности не превышает 3."""
    monkeypatch.setenv('RT_ENRICH_CONCURRENCY', '3')
    omdb = _FakeOmdbClient(delay=0.05)
    movies = [_movie(f'tt{i:07d}') for i in range(1, 14)]
    asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies, client=omdb))
    assert len(omdb.calls) == 13
    assert 2 <= omdb.peak <= 3


def test_enrichment_is_parallel(flag_on, fake_cache):
    """Обращения идут параллельно: суммарное время меньше последовательного."""
    delay = 0.15
    omdb = _FakeOmdbClient(delay=delay)
    movies = [_movie(f'tt{i:07d}') for i in range(1, 7)]

    async def _run():
        started = time.monotonic()
        await rt_enrichment.enrich_movies_with_rt_scores(None, movies, client=omdb)
        return time.monotonic() - started

    elapsed = asyncio.run(_run())
    # Последовательно было бы 6 * 0.15 = 0.9 c; параллельно (лимит 5) — 2 волны
    assert elapsed < 5 * delay
    assert omdb.peak > 1


def test_mixed_list_maps_scores_to_own_movies(flag_on, fake_cache):
    """Смешанный список: оценки сопоставлены своим фильмам, порядок цел (D8)."""
    fake_cache.hits['tt0000001'] = CacheLookup(hit=True, scores=RtScores(rt_score=10, metascore=20))
    omdb = _FakeOmdbClient(results={'tt0000003': RtScores(rt_score=30, metascore=40)}, delay=0.01)
    movies = [
        _movie('tt0000001', title='Из кэша'),
        _movie(None, title='Без ID'),
        _movie('tt0000003', title='Из источника'),
        _movie('tt0000001', title='Дубликат из кэша'),
    ]
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies, client=omdb))
    assert [m['title'] for m in result] == ['Из кэша', 'Без ID', 'Из источника', 'Дубликат из кэша']
    assert (result[0]['rt_score'], result[0]['metascore']) == (10, 20)
    assert (result[1]['rt_score'], result[1]['metascore']) == (None, None)
    assert (result[2]['rt_score'], result[2]['metascore']) == (30, 40)
    assert (result[3]['rt_score'], result[3]['metascore']) == (10, 20)
    assert omdb.calls == ['tt0000003']
    assert fake_cache.set_calls == [('tt0000003', RtScores(rt_score=30, metascore=40))]


# --- R5: общий дедлайн ---


def test_deadline_exceeded_returns_movies_without_scores(flag_on, fake_cache, monkeypatch):
    """Медленный источник: выдача цела, поля None, исключение не пробрасывается."""
    monkeypatch.setenv('RT_ENRICH_DEADLINE_SECONDS', '0.2')
    omdb = _FakeOmdbClient(delay=5.0)
    movies = [_movie(f'tt{i:07d}') for i in range(1, 4)]

    async def _run():
        started = time.monotonic()
        result = await rt_enrichment.enrich_movies_with_rt_scores(None, movies, client=omdb)
        return time.monotonic() - started, result

    elapsed, result = asyncio.run(_run())
    assert elapsed < 2.0
    assert result is movies and len(result) == 3
    assert all(m['rt_score'] is None and m['metascore'] is None for m in movies)


def test_blocking_cache_thread_is_cut_by_deadline(flag_on, monkeypatch):
    """D6/R5: блокирующий поток «к недоступной БД» отсекается дедлайном.

    Фейковый aget_scores для одного фильма уходит в asyncio.to_thread
    (time.sleep) — как реальное чтение кэша при недоступном PostgreSQL.
    Выдача возвращается по дедлайну; частичный результат (быстрый фильм)
    сохраняется.
    """
    monkeypatch.setenv('RT_ENRICH_DEADLINE_SECONDS', '0.2')

    async def _blocking_get(imdb_id: Optional[str], app: Any = None) -> CacheLookup:
        if imdb_id == 'tt0000002':
            await asyncio.to_thread(time.sleep, 1.0)
        return rt_cache.MISS

    written: List[Any] = []

    async def _fake_set(imdb_id: Optional[str], scores: Optional[RtScores], app: Any = None) -> None:
        written.append((imdb_id, scores))

    monkeypatch.setattr(rt_enrichment, 'aget_scores', _blocking_get)
    monkeypatch.setattr(rt_enrichment, 'aset_scores', _fake_set)
    omdb = _FakeOmdbClient()
    movies = [_movie('tt0000001', title='Быстрый'), _movie('tt0000002', title='Блокирующий')]

    async def _run():
        started = time.monotonic()
        result = await rt_enrichment.enrich_movies_with_rt_scores(None, movies, client=omdb)
        return time.monotonic() - started, result

    elapsed, result = asyncio.run(_run())
    # Дедлайн 0.2 c отсекает ожидание потока (sleep 1.0 c) — выдача вовремя
    assert elapsed < 0.9
    assert result is movies
    assert movies[0]['rt_score'] == 91  # частичный результат сохранён
    assert movies[1]['rt_score'] is None and movies[1]['metascore'] is None


# --- R6: прозрачная деградация при любом сбое ---


def test_single_movie_failure_does_not_break_others(flag_on, monkeypatch):
    """Сбой кэша по одному фильму — остальные обогащены, выдача цела."""
    cache = _FakeCache(get_raises={'tt0000002': RuntimeError('БД недоступна')})
    monkeypatch.setattr(rt_enrichment, 'aget_scores', cache.aget_scores)
    monkeypatch.setattr(rt_enrichment, 'aset_scores', cache.aset_scores)
    omdb = _FakeOmdbClient()
    movies = [_movie('tt0000001'), _movie('tt0000002'), _movie('tt0000003')]
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies, client=omdb))
    assert len(result) == 3
    assert result[0]['rt_score'] == 91
    assert result[1]['rt_score'] is None and result[1]['metascore'] is None
    assert result[2]['rt_score'] == 91


def test_cache_exception_does_not_break_delivery(flag_on, monkeypatch):
    """Чтение и запись кэша недоступны — выдача целиком с пустыми оценками."""

    async def _boom_get(imdb_id: Optional[str], app: Any = None) -> CacheLookup:
        raise RuntimeError('хранилище кэша недоступно')

    async def _boom_set(imdb_id: Optional[str], scores: Optional[RtScores], app: Any = None) -> None:
        raise RuntimeError('хранилище кэша недоступно')

    monkeypatch.setattr(rt_enrichment, 'aget_scores', _boom_get)
    monkeypatch.setattr(rt_enrichment, 'aset_scores', _boom_set)
    movies = [_movie(f'tt{i:07d}') for i in range(1, 4)]
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(
        None, movies, client=_FakeOmdbClient()
    ))
    assert result is movies and len(result) == 3
    assert all(m['rt_score'] is None and m['metascore'] is None for m in movies)


def test_omdb_exception_does_not_break_delivery(flag_on, fake_cache):
    """Исключение вместо результата источника — фильм без оценок, соседи целы."""
    omdb = _FakeOmdbClient(results={'tt0000002': RuntimeError('сеть недоступна')})
    movies = [_movie('tt0000001'), _movie('tt0000002'), _movie('tt0000003')]
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies, client=omdb))
    assert len(result) == 3
    assert result[0]['rt_score'] == 91
    assert result[1]['rt_score'] is None and result[1]['metascore'] is None
    assert result[2]['rt_score'] == 91
    # Исключение перехвачено per-item контуром (D7) до записи в кэш:
    # сбой источника не кэшируется вовсе
    assert all(imdb_id != 'tt0000002' for imdb_id, _scores in fake_cache.set_calls)


def test_empty_list_is_noop(flag_on, sentinel_cache, monkeypatch):
    """Пустой список — no-op: ноль обращений, тот же список, без исключений."""
    created = _install_sentinel_client(monkeypatch)
    movies: List[Dict[str, Any]] = []
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies))
    assert result is movies == []
    assert sentinel_cache == []
    assert created == []


# --- R7: feature flag ---


def test_flag_off_no_network_no_db(flag_off, sentinel_cache, monkeypatch):
    """Флаг выключен: ноль обращений к источнику и кэшу, клиент не создан."""
    created = _install_sentinel_client(monkeypatch)
    movies = [_movie('tt0111161'), _movie('tt0111162')]
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies))
    assert result is movies
    assert all(m['rt_score'] is None and m['metascore'] is None for m in movies)
    assert created == []
    assert sentinel_cache == []


def test_flag_on_without_key_is_noop(monkeypatch, sentinel_cache):
    """ENABLE_RT_SCORES=true без ключа OMDb — флаг неактивен, no-op (R7)."""
    monkeypatch.setenv('ENABLE_RT_SCORES', 'true')
    monkeypatch.setenv('OMDB_API_KEY', '')
    created = _install_sentinel_client(monkeypatch)
    movies = [_movie('tt0111161')]
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies))
    assert result[0]['rt_score'] is None
    assert created == []
    assert sentinel_cache == []


def test_flag_on_runs_pipeline(flag_on, fake_cache):
    """Флаг активен — полный пайплайн: кэш -> источник -> запись в кэш."""
    omdb = _FakeOmdbClient()
    movies = [_movie('tt0111161')]
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies, client=omdb))
    assert fake_cache.get_calls == ['tt0111161']
    assert omdb.calls == ['tt0111161']
    assert fake_cache.set_calls == [('tt0111161', DEFAULT_SCORES)]
    assert result[0]['rt_score'] == 91
    assert result[0]['metascore'] == 82


def test_make_client_failure_is_silent(flag_on, sentinel_cache, monkeypatch, caplog):
    """Регрессия D7 (фикс minor ревью B5): сбой _make_client поглощается.

    Клиент создаётся внутри внешнего try: неожиданное исключение не
    пробрасывается вызывающему коду, выдача возвращается с rt_score=None,
    обращений к кэшу нет (сбой происходит до запуска _enrich_all).
    """
    def _boom():
        raise RuntimeError('сбой конфигурации')

    monkeypatch.setattr(rt_enrichment, '_make_client', _boom)
    movies = [_movie('tt0111161')]
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies))
    assert result is movies
    assert result[0]['rt_score'] is None
    assert result[0]['metascore'] is None
    assert sentinel_cache == []
    assert 'RuntimeError' in caplog.text


# --- R2/R8: ресурсы вызова — клиент, сессия ---


def test_single_client_per_call(flag_on, fake_cache, monkeypatch):
    """Один экземпляр клиента на вызов (13 фильмов), ключ — из config."""
    created: List[Any] = []

    class _TrackingClient(_FakeOmdbClient):
        def __init__(self, api_key: str):
            super().__init__()
            self.api_key = api_key
            created.append(self)

    monkeypatch.setattr(rt_enrichment, 'OmdbClient', _TrackingClient)
    movies = [_movie(f'tt{i:07d}') for i in range(1, 14)]
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(None, movies))
    assert len(created) == 1
    assert created[0].api_key == 'test-omdb-key'
    assert len(created[0].calls) == 13
    assert all(m['rt_score'] == 91 for m in result)


def test_caller_session_is_reused(flag_on, fake_cache, monkeypatch):
    """Сессия вызывающего кода переиспользуется; новая не создаётся (D11)."""
    sentinel = object()  # «сессия» вызывающего кода
    omdb = _FakeOmdbClient()

    def _no_new_session(*args: Any, **kwargs: Any):
        raise AssertionError('Новая aiohttp-сессия создаваться не должна')

    monkeypatch.setattr(aiohttp, 'ClientSession', _no_new_session)
    movies = [_movie(f'tt{i:07d}') for i in range(1, 4)]
    result = asyncio.run(rt_enrichment.enrich_movies_with_rt_scores(sentinel, movies, client=omdb))
    assert len(omdb.sessions) == 3
    assert all(s is sentinel for s in omdb.sessions)
    assert all(m['rt_score'] == 91 for m in result)


# --- 2.1/2.2 / R1: сквозные тесты путей выдачи движка ---


def test_range_path_enriches_only_final_list(flag_on, fake_cache, monkeypatch):
    """Диапазонный путь: 40 кандидатов -> limit=13 -> ровно 13 обращений."""
    docs = [_kp_doc(i, f'tt{i:07d}') for i in range(1, 41)]
    engine = RecommendationEngine(_StubKinopoiskClient(docs))
    omdb = _FakeOmdbClient()
    _install_fake_client(monkeypatch, omdb)
    result = asyncio.run(engine.get_recommendations(
        session=None, year_range=(2015, 2020), min_imdb_rating=6.0, limit=13
    ))
    assert len(result) == 13
    # Кандидаты (до 250) не обогащаются — только финал после обрезки
    assert len(omdb.calls) == 13
    assert set(omdb.calls) == {m['imdb_id'] for m in result}
    assert all(m['rt_score'] == 91 and m['metascore'] == 82 for m in result)


def test_general_path_enriches_only_final_list(flag_on, fake_cache, monkeypatch):
    """Общий путь: 40 кандидатов -> limit=13 -> ровно 13 обращений."""
    docs = [_kp_doc(i, f'tt{i:07d}') for i in range(1, 41)]
    engine = RecommendationEngine(_StubKinopoiskClient(docs))
    omdb = _FakeOmdbClient()
    _install_fake_client(monkeypatch, omdb)
    result = asyncio.run(engine.get_recommendations(
        session=None, min_imdb_rating=6.0, limit=13
    ))
    assert len(result) == 13
    assert len(omdb.calls) == 13
    assert set(omdb.calls) == {m['imdb_id'] for m in result}
    assert all(m['rt_score'] == 91 and m['metascore'] == 82 for m in result)


def test_final_smaller_than_limit_enriches_available_only(flag_on, fake_cache, monkeypatch):
    """Финал меньше лимита: 3 фильма, 2 с IMDb ID — не более 2 обращений."""
    docs = [_kp_doc(1, 'tt0000001'), _kp_doc(2, None), _kp_doc(3, 'tt0000003')]
    engine = RecommendationEngine(_StubKinopoiskClient(docs))
    omdb = _FakeOmdbClient()
    _install_fake_client(monkeypatch, omdb)
    result = asyncio.run(engine.get_recommendations(
        session=None, min_imdb_rating=6.0, limit=13
    ))
    assert len(result) == 3
    assert sorted(omdb.calls) == ['tt0000001', 'tt0000003']
    without_id = [m for m in result if m['imdb_id'] is None]
    assert len(without_id) == 1
    assert without_id[0]['rt_score'] is None and without_id[0]['metascore'] is None


# --- 2.3: карточка search_by_title (info-интент) ---


def test_search_by_title_card_is_enriched(flag_on, fake_cache, monkeypatch):
    """Карточка фильма, найденного по названию, обогащается RT-оценками."""
    agent = MovieAgent()
    agent.kinopoisk_client.search_movie_by_title = AsyncMock(
        return_value={'docs': [_kp_doc(326, 'tt0111161')]}
    )
    omdb = _FakeOmdbClient(results={'tt0111161': RtScores(rt_score=77, metascore=66)})
    _install_fake_client(monkeypatch, omdb)
    result = asyncio.run(agent.search_by_title(session=None, title='Фильм 326'))
    assert len(result) == 1
    card = result[0]
    assert card['imdb_id'] == 'tt0111161'
    assert card['rt_score'] == 77
    assert card['metascore'] == 66
    assert omdb.calls == ['tt0111161']
    assert fake_cache.set_calls == [('tt0111161', RtScores(rt_score=77, metascore=66))]


# --- 2.4: карточка handle_movie_detail (callback «Подробнее») ---


def test_detail_card_is_enriched(flag_on, fake_cache, monkeypatch):
    """Хендлер бота обогащает карточку до сборки текста ответа (D12)."""
    omdb = _FakeOmdbClient(results={'tt0111161': RtScores(rt_score=91, metascore=82)})
    _install_fake_client(monkeypatch, omdb)
    enriched: List[Dict[str, Any]] = []

    async def _spy(session: Any, movies: List[Dict[str, Any]], client: Any = None):
        result = await rt_enrichment.enrich_movies_with_rt_scores(session, movies, client=client)
        enriched.extend(result)
        return result

    monkeypatch.setattr(telegram_bot, 'enrich_movies_with_rt_scores', _spy)

    payload = {
        'id': 326,
        'name': 'Побег из Шоушенка',
        'year': 1994,
        'genres': [{'name': 'драма'}],
        'countries': [{'name': 'США'}],
        'rating': {'imdb': 9.3, 'kp': 9.1},
        'description': 'Описание',
        'poster': {'url': 'https://example.com/poster.jpg'},
        'externalId': {'imdb': 'tt0111161'},
        'type': 'movie',
    }
    fake_session = _FakeHttpSession(payload)
    monkeypatch.setattr(aiohttp, 'ClientSession', lambda: fake_session)

    query = AsyncMock()
    query.data = 'info:326'
    update = SimpleNamespace(callback_query=query)
    asyncio.run(telegram_bot.handle_movie_detail(update, context=None))

    assert len(enriched) == 1
    assert enriched[0]['rt_score'] == 91
    assert enriched[0]['metascore'] == 82
    assert omdb.calls == ['tt0111161']
    assert fake_cache.set_calls == [('tt0111161', RtScores(rt_score=91, metascore=82))]
    # A4: карточка с валидным постером доставляется ОДНИМ сообщением —
    # reply_photo с caption (отдельного текстового сообщения больше нет),
    # а обогащение доведено до пользователя: бейдж «🍅 91%» в caption
    query.message.reply_photo.assert_awaited_once()
    photo_kwargs = query.message.reply_photo.await_args.kwargs
    assert '🍅 91%' in photo_kwargs['caption']
    assert photo_kwargs['parse_mode'] == 'HTML'
    assert photo_kwargs['photo'] == 'https://example.com/poster.jpg'
    query.message.reply_text.assert_not_awaited()
