# tests/test_omdb_client.py
"""Тесты B2: клиент OMDb — парсинг Ratings[] и контракт fail-silent.

Без сети: aiohttp-сессия заменена фейком, возвращающим готовые payload'ы
или исключения. Флаг Epic B задаётся через monkeypatch (rt_scores_enabled
читает env в момент вызова — свойство B1), паузы tenacity отключены.
conftest.py уже добавляет src/ в sys.path.
"""
import asyncio
import json
import logging

import aiohttp
import pytest

from omdb_client import OmdbClient, RtScores, _parse_int_or_none, _parse_percent


class _FakeResponse:
    """Фейковый ответ aiohttp: async context manager со status и json()."""

    def __init__(self, payload=None, status=200, broken_json=False):
        self.status = status
        self._payload = payload
        self._broken_json = broken_json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def json(self, content_type=None):
        if self._broken_json:
            raise json.JSONDecodeError('Expecting value', '<doc>', 0)
        return self._payload


class _FakeSession:
    """Фейковая сессия aiohttp: записывает обращения и отдаёт готовые ответы.

    Аргументы — очередь из _FakeResponse или исключений (по одному на запрос).
    Исчерпание очереди — AssertionError: защита от незапланированных повторов.
    """

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None, **kwargs):
        self.calls.append({'url': url, 'params': params, 'timeout': timeout})
        if not self._responses:
            raise AssertionError('Незапланированный дополнительный запрос к OMDb')
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            # Реальная сессия бросает сетевую ошибку ещё до получения ответа
            raise item
        return item


@pytest.fixture(autouse=True)
def fast_retry_sleep(monkeypatch):
    """Отключить паузы tenacity: тесты не должны ждать экспоненциальный delay."""
    async def _instant_sleep(seconds):
        return None

    monkeypatch.setattr(asyncio, 'sleep', _instant_sleep)


@pytest.fixture
def flag_on(monkeypatch):
    """Включить feature flag Epic B (env читается в момент вызова)."""
    monkeypatch.setenv('ENABLE_RT_SCORES', 'true')
    monkeypatch.setenv('OMDB_API_KEY', 'test-omdb-key')


def _rt_payload():
    """Типичный ответ OMDb с Rotten Tomatoes «91%» и Metascore «82»."""
    return {
        'Title': 'The Shawshank Redemption',
        'Response': 'True',
        'Ratings': [
            {'Source': 'Internet Movie Database', 'Value': '9.3/10'},
            {'Source': 'Rotten Tomatoes', 'Value': '91%'},
            {'Source': 'Metacritic', 'Value': '82/100'},
        ],
        'Metascore': '82',
    }


def _fetch(session, imdb_id='tt0111161'):
    """Вызов fetch_rt_scores через asyncio.run (клиент — тестовый ключ)."""
    client = OmdbClient('test-omdb-key')
    return asyncio.run(client.fetch_rt_scores(session, imdb_id))


# --- 3.1: успешный парсинг и параметры запроса ---


def test_rt_and_metascore_parsed(flag_on):
    """«91%» -> 91, Metascore «82» -> 82; запрос уходит с i и apikey."""
    session = _FakeSession(_FakeResponse(payload=_rt_payload()))
    result = _fetch(session)
    assert result == RtScores(rt_score=91, metascore=82)
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call['url'] == 'https://www.omdbapi.com/'
    assert call['params'] == {'i': 'tt0111161', 'apikey': 'test-omdb-key'}


# --- 3.2: хелперы парсинга и устойчивые к мусору значения ---


def test_parse_percent():
    assert _parse_percent('91%') == 91
    assert _parse_percent(' 89 % ') == 89
    assert _parse_percent('N/A') is None
    assert _parse_percent('') is None
    assert _parse_percent(None) is None
    assert _parse_percent(91) is None  # не-строка не парсится


def test_parse_int_or_none():
    assert _parse_int_or_none('82') == 82
    assert _parse_int_or_none(82) == 82
    assert _parse_int_or_none('N/A') is None
    assert _parse_int_or_none('') is None
    assert _parse_int_or_none(None) is None
    assert _parse_int_or_none(True) is None


def test_metascore_na(flag_on):
    """Metascore «N/A» -> None, оценка RT при этом сохраняется."""
    payload = {
        'Response': 'True',
        'Ratings': [{'Source': 'Rotten Tomatoes', 'Value': '89%'}],
        'Metascore': 'N/A',
    }
    result = _fetch(_FakeSession(_FakeResponse(payload=payload)))
    assert result == RtScores(rt_score=89, metascore=None)


def test_broken_rt_value_keeps_metascore(flag_on):
    """Некорректный Value у RT -> rt_score None, metascore сохраняется."""
    payload = {
        'Response': 'True',
        'Ratings': [{'Source': 'Rotten Tomatoes', 'Value': 'N/A'}],
        'Metascore': '80',
    }
    result = _fetch(_FakeSession(_FakeResponse(payload=payload)))
    assert result == RtScores(rt_score=None, metascore=80)


# --- 3.3: нет RT в Ratings и Response=False ---


def test_no_rt_source_returns_empty_rt(flag_on):
    """Response=True, но источника Rotten Tomatoes нет -> rt_score None (D5)."""
    payload = {
        'Response': 'True',
        'Ratings': [
            {'Source': 'Internet Movie Database', 'Value': '8.8/10'},
            'мусор',  # некорректный элемент безопасно пропускается
        ],
        'Metascore': '70',
    }
    result = _fetch(_FakeSession(_FakeResponse(payload=payload)))
    assert result == RtScores(rt_score=None, metascore=70)


def test_response_false_returns_none(flag_on):
    """Response=False (фильм не найден) -> None без повторных запросов."""
    payload = {'Response': 'False', 'Error': 'Movie not found!'}
    session = _FakeSession(_FakeResponse(payload=payload))
    assert _fetch(session) is None
    assert len(session.calls) == 1


# --- 3.4: fail-silent при сбоях ---


def test_network_error_fail_silent(flag_on):
    """Сетевая ошибка на обеих попытках -> None, исключение не проброшено."""
    err = aiohttp.ClientConnectionError('Соединение недоступно')
    session = _FakeSession(err, err)
    assert _fetch(session) is None
    assert len(session.calls) == 2  # умеренный retry: ровно 2 попытки


def test_timeout_fail_silent(flag_on):
    """Таймаут на обеих попытках -> None (fail-silent)."""
    session = _FakeSession(asyncio.TimeoutError(), asyncio.TimeoutError())
    assert _fetch(session) is None
    assert len(session.calls) == 2


def test_broken_json_fail_silent(flag_on):
    """Некорректный JSON -> None; такие ответы НЕ повторяются (экономия квоты)."""
    session = _FakeSession(_FakeResponse(broken_json=True))
    assert _fetch(session) is None
    assert len(session.calls) == 1


# --- 3.5: retry-политика ---


def test_retry_once_then_success(flag_on):
    """Первая попытка — сбой, вторая — успех: результат получен, 2 запроса."""
    err = aiohttp.ClientConnectionError('Временный сбой')
    session = _FakeSession(err, _FakeResponse(payload=_rt_payload()))
    result = _fetch(session)
    assert result == RtScores(rt_score=91, metascore=82)
    assert len(session.calls) == 2


def test_http_error_not_retried(flag_on):
    """HTTP-статус != 200 не повторяется (OMDb и при квоте отвечает 200)."""
    session = _FakeSession(_FakeResponse(status=503))
    assert _fetch(session) is None
    assert len(session.calls) == 1


# --- 3.6: feature flag ---


def test_flag_off_no_network(monkeypatch):
    """Флаг выключен -> None без единого обращения к сессии."""
    monkeypatch.setenv('ENABLE_RT_SCORES', 'false')
    monkeypatch.setenv('OMDB_API_KEY', 'test-omdb-key')
    session = _FakeSession()  # любое обращение — AssertionError
    assert _fetch(session) is None
    assert session.calls == []


def test_flag_on_but_key_blank_no_network(monkeypatch):
    """Флаг включён, но ключ пуст -> Epic B выключен, сети нет."""
    monkeypatch.setenv('ENABLE_RT_SCORES', 'true')
    monkeypatch.setenv('OMDB_API_KEY', '   ')
    session = _FakeSession()
    assert _fetch(session) is None
    assert session.calls == []


# --- 3.7: пустой IMDb ID ---


@pytest.mark.parametrize('imdb_id', [None, '', '   '])
def test_empty_imdb_id_no_network(flag_on, imdb_id):
    """Пустой/None IMDb ID -> None без сетевого запроса."""
    session = _FakeSession()
    assert _fetch(session, imdb_id) is None
    assert session.calls == []


# --- 3.8: таймаут запроса ---


def test_request_timeout_within_bounds(flag_on):
    """В session.get передан aiohttp.ClientTimeout в диапазоне 5–10 с."""
    session = _FakeSession(_FakeResponse(payload=_rt_payload()))
    _fetch(session)
    timeout = session.calls[0]['timeout']
    assert isinstance(timeout, aiohttp.ClientTimeout)
    assert timeout.total == 8
    assert 5 <= timeout.total <= 10


# --- 3.9: покрытие парсинга «мусорных» ответов (minor #4 ревью B2) ---


def test_ratings_not_list_gives_none_rt(flag_on):
    """Ratings — не список (строка-мусор) -> rt_score None, metascore цел."""
    payload = {'Response': 'True', 'Ratings': 'мусор', 'Metascore': '77'}
    result = _fetch(_FakeSession(_FakeResponse(payload=payload)))
    assert result == RtScores(rt_score=None, metascore=77)


def test_missing_metascore_key_gives_none(flag_on):
    """Ключ Metascore отсутствует вовсе -> metascore None (не ошибка)."""
    payload = {'Response': 'True', 'Ratings': [{'Source': 'Rotten Tomatoes', 'Value': '88%'}]}
    result = _fetch(_FakeSession(_FakeResponse(payload=payload)))
    assert result == RtScores(rt_score=88, metascore=None)


def test_payload_not_dict_returns_none(flag_on):
    """Payload не является словарём (например список) -> None без исключений."""
    session = _FakeSession(_FakeResponse(payload=[{'Response': 'True'}]))
    assert _fetch(session) is None
    assert len(session.calls) == 1


# --- 3.10: логирование и маска API-ключа (minor #1–#3 ревью B2) ---


def test_response_false_logs_warning(flag_on, caplog):
    """Response='False' -> запись WARNING с префиксом [OMDb] и IMDb ID."""
    caplog.set_level(logging.WARNING)
    payload = {'Response': 'False', 'Error': 'Movie not found!'}
    assert _fetch(_FakeSession(_FakeResponse(payload=payload))) is None
    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert records, 'Ожидалась хотя бы одна запись уровня WARNING'
    message = records[-1].getMessage()
    assert '[OMDb]' in message
    assert 'tt0111161' in message


def test_network_error_logs_error_after_retries(flag_on, caplog):
    """Сетевая ошибка после исчерпания retry -> запись ERROR с [OMDb]."""
    caplog.set_level(logging.WARNING)
    err = aiohttp.ClientConnectionError('Соединение недоступно')
    assert _fetch(_FakeSession(err, err)) is None
    records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert records, 'Ожидалась запись уровня ERROR после исчерпания попыток'
    assert '[OMDb]' in records[-1].getMessage()


def test_api_key_masked_in_logs(flag_on, caplog):
    """Ключ не утекает в логи: str(exc) с apikey= маскируется в '***'."""
    caplog.set_level(logging.WARNING)
    secret = 'test-secret-key'
    # Реальная aiohttp-ошибка такого вида несёт полный URL с параметром apikey=
    err = aiohttp.ClientConnectionError(
        f'Cannot connect to host www.omdbapi.com:443 ssl:default [apikey={secret}]'
    )
    client = OmdbClient(secret)
    session = _FakeSession(err, err)
    assert asyncio.run(client.fetch_rt_scores(session, 'tt0111161')) is None
    assert caplog.records, 'Ожидались записи в логе (retry + финальная ошибка)'
    assert secret not in caplog.text
    assert '***' in caplog.text


def test_http_error_logs_single_record(flag_on, caplog):
    """HTTP != 200: ровно одна запись [OMDb] (ERROR), без дубля WARNING."""
    caplog.set_level(logging.WARNING)
    assert _fetch(_FakeSession(_FakeResponse(status=503))) is None
    records = [r for r in caplog.records if '[OMDb]' in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR


def test_mask_helper():
    """_mask заменяет ключ на '***'; при пустом ключе текст не меняется."""
    assert OmdbClient('secret')._mask('apikey=secret&i=tt0111161') == 'apikey=***&i=tt0111161'
    assert OmdbClient('')._mask('apikey=') == 'apikey='

