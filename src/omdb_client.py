# src/omdb_client.py
"""Клиент OMDb API: оценки Rotten Tomatoes и Metacritic по IMDb ID.

Epic B, задача B2. Контракт fail-silent: любой сбой (сеть, таймаут,
некорректный JSON, Response != "True") приводит к возврату None — исключение
никогда не пробрасывается в вызывающий код. Feature flag Epic B
(config.rt_scores_enabled) проверяется в момент вызова: при выключенном
флаге сетевые запросы не выполняются вовсе.

Retry-политика намеренно умеренная (2 попытки, только транзиентные сетевые
ошибки): лимит free-ключа OMDb — 1000 запросов/день, агрессивные повторы
могут исчерпать дневную квоту и оставить все выдачи без оценок.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp
from tenacity import RetryCallState, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import rt_scores_enabled

logger = logging.getLogger(__name__)

# Базовый URL OMDb API: GET /?i={imdb_id}&apikey={ключ}
OMDB_URL = 'https://www.omdbapi.com/'

# Таймаут запроса в секундах (по задаче B2 допустимо 5–10 с)
OMDB_TIMEOUT = 8


@dataclass(frozen=True, slots=True)
class RtScores:
    """Оценки фильма из ответа OMDb.

    rt_score — Tomatometer (0–100), metascore — оценка Metacritic (0–100).
    None в поле означает «источник не дал оценку» — это не ошибка.
    """

    rt_score: Optional[int] = None
    metascore: Optional[int] = None


def _parse_percent(value: Any) -> Optional[int]:
    """Разбор процентной оценки: "91%" -> 91.

    Любой мусор ("N/A", пустая строка, нечисло, не-строка) -> None,
    без исключений.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().rstrip('%').strip()
    try:
        return int(text)
    except ValueError:
        return None


def _parse_int_or_none(value: Any) -> Optional[int]:
    """Разбор целочисленного поля OMDb: "82" -> 82.

    "N/A", None, пустая строка и нечисловые значения -> None, без исключений.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _extract_rt_score(ratings: Any) -> Optional[int]:
    """Извлечение оценки Rotten Tomatoes из Ratings[] ответа OMDb.

    Ratings — список словарей вида {"Source": ..., "Value": "91%"};
    не-список и некорректные элементы безопасно пропускаются.
    """
    if not isinstance(ratings, list):
        return None
    for item in ratings:
        if not isinstance(item, dict):
            continue
        if item.get('Source') == 'Rotten Tomatoes':
            return _parse_percent(item.get('Value'))
    return None


def _parse_response(data: Any, imdb_id: str) -> Optional[RtScores]:
    """Разбор ответа OMDb в RtScores.

    Response != "True" (фильм не найден, невалидный ключ, исчерпан дневной
    лимит) -> None. Отсутствие Rotten Tomatoes или Metacritic -> None
    в соответствующем поле структуры (не ошибка, деградация незаметна).
    """
    if not isinstance(data, dict):
        logger.warning(f"[OMDb] Неожиданный формат ответа для фильма {imdb_id}")
        return None
    if str(data.get('Response')).lower() != 'true':
        error = data.get('Error') or 'причина неизвестна'
        logger.warning(f"[OMDb] Нет данных для фильма {imdb_id}: {error}")
        return None
    return RtScores(
        rt_score=_extract_rt_score(data.get('Ratings')),
        metascore=_parse_int_or_none(data.get('Metascore')),
    )


def _log_retry(retry_state: RetryCallState) -> None:
    """Логирование перед повторной попыткой (хук tenacity before_sleep).

    Намеренно пишем только имя типа исключения: у подклассов aiohttp
    ClientResponseError (TooManyRedirects, ClientHttpProxyError) str(exc)
    содержит полный URL запроса вместе с `apikey=`, то есть секрет.
    """
    outcome = retry_state.outcome
    exc = outcome.exception() if outcome is not None else None
    reason = type(exc).__name__ if exc is not None else 'неизвестная ошибка'
    logger.warning(f"[OMDb] Повторная попытка {retry_state.attempt_number}/2, причина: {reason}")


class OmdbClient:
    """Асинхронный клиент OMDb API (оценки Rotten Tomatoes по IMDb ID).

    По образцу KinopoiskClient: API-ключ передаётся в конструктор, сессия
    aiohttp — во внешний метод (владеет сессией вызывающий код). Публичный
    метод fetch_rt_scores никогда не бросает исключений (fail-silent).
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = OMDB_URL

    def _mask(self, text: str) -> str:
        """Маскировка API-ключа в тексте сообщения лога.

        str() исключений aiohttp (например TooManyRedirects или
        ClientHttpProxyError) содержит полный URL запроса вместе с параметром
        `apikey=` — секрет не должен попадать в логи. Пустой ключ не
        маскируется: заменять нечего.
        """
        if not self.api_key:
            return text
        return text.replace(self.api_key, '***')

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=3),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        before_sleep=_log_retry,
        reraise=True,
    )
    async def _fetch(self, session: aiohttp.ClientSession, imdb_id: str) -> Optional[dict]:
        """Сетевой запрос к OMDb; повторяется только при транзиентных сбоях.

        HTTP-статусы != 200 и некорректный JSON НЕ повторяются: OMDb даже
        при невалидном ключе/исчерпанной квоте отвечает 200 с Response=False,
        а повторные попытки тратят дневной лимит free-ключа.
        """
        params = {'i': imdb_id, 'apikey': self.api_key}
        async with session.get(
            self.base_url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=OMDB_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                logger.error(f"[OMDb] HTTP {resp.status} при запросе фильма {imdb_id}")
                return None
            # content_type=None — защита от нестандартного Content-Type;
            # некорректный JSON бросает JSONDecodeError (не повторяется)
            return await resp.json(content_type=None)

    async def fetch_rt_scores(
        self,
        session: aiohttp.ClientSession,
        imdb_id: Optional[str],
    ) -> Optional[RtScores]:
        """Оценки Rotten Tomatoes/Metacritic для фильма по IMDb ID.

        Возвращает None без сетевого запроса, если feature flag Epic B
        выключен или imdb_id пуст; None при любом сбое (сеть, таймаут,
        парсинг). Исключения не пробрасываются — вызывающий код не обязан
        их обрабатывать.
        """
        if not rt_scores_enabled():
            # Флаг Epic B выключен — не тратим ни запросы, ни дневной лимит
            logger.debug('[OMDb] RT-оценки выключены feature flag, запрос пропущен')
            return None
        if not imdb_id or not str(imdb_id).strip():
            logger.debug('[OMDb] Запрос пропущен: пустой IMDb ID')
            return None
        imdb_id = str(imdb_id).strip()
        try:
            data = await self._fetch(session, imdb_id)
            if data is None:
                # HTTP-статус != 200 уже залогирован внутри _fetch —
                # не дублируем запись WARNING «Неожиданный формат ответа»
                return None
            return _parse_response(data, imdb_id)
        except Exception as exc:
            # Сеть/таймаут после исчерпания попыток или сбой парсинга —
            # тихая деградация: выдача рекомендаций просто будет без бейджей.
            # str(exc) может содержать URL с apikey=, поэтому маскируем.
            detail = self._mask(str(exc)) or type(exc).__name__
            logger.error(f"[OMDb] Ошибка получения оценок для фильма {imdb_id}: {detail}")
            return None
