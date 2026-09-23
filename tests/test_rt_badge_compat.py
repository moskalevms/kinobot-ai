"""Проверки совместимости бейджа Tomatometer (B7, add-rt-badge).

Без сети: проверяются отсутствие эмодзи в логах (консоль cp1251 — ловушка
AGENTS.md), безопасность текста бейджа для HTML-рендера Telegram и веба
(`src/templates/index.html` вставляет ответ через innerHTML), отсутствие
дубля формата карточки в `telegram_bot.py` и прямой сценарий callback
«Подробнее» (`telegram_bot.handle_movie_detail`, minor-замечание ревью B7,
закрывается в B8/verify-rt-phase1).
"""
import asyncio
import json
import logging
from typing import Any, List, Tuple

from dialogue_manager import (
    DialogueManager,
    format_movie_card,
    format_rt_badge,
)
from session_manager import UserSession


class _StubSessionManager:
    """In-memory заглушка менеджера сессий (без БД)."""

    def __init__(self):
        self.sessions = {}

    def get_session(self, user_id):
        if user_id not in self.sessions:
            self.sessions[user_id] = UserSession(user_id)
        return self.sessions[user_id]

    def save_session(self, session):
        return None

    def clear_session(self, user_id):
        self.sessions.pop(user_id, None)


def _movie(**overrides):
    movie = {
        'id': 447301, 'title': 'Начало', 'year': 2010, 'genre': 'фантастика',
        'rating': 8.8, 'description': 'Сон внутри сна',
        'rt_score': 91, 'metascore': 82,
    }
    movie.update(overrides)
    return movie


def test_badge_is_not_logged(caplog):
    """Форматирование выдачи с бейджами не порождает эмодзи в логах."""
    with caplog.at_level(logging.DEBUG):
        # Инициализация внутри блока: её записи подтверждают, что капчер
        # логов работает (иначе тест был бы vacuous)
        dm = DialogueManager(_StubSessionManager())
        dm._generate_list_response([_movie(), _movie(rt_score=None)], 'Заголовок')
        format_movie_card(_movie())

    assert caplog.records, 'ожидаются записи лога на пути формирования выдачи'
    for record in caplog.records:
        assert '🍅' not in record.getMessage()
    assert '🍅' not in caplog.text


def test_badge_text_is_html_safe():
    """Текст бейджа не ломает HTML Telegram и innerHTML веб-интерфейса."""
    badge = format_rt_badge(_movie())

    assert badge == '🍅 91%'
    for char in '<>&"\'':
        assert char not in badge


def test_card_with_badge_is_html_safe_and_json_serializable():
    """Карточка с бейджем сериализуется в JSON ответа /chat без искажений."""
    card = format_movie_card(_movie())

    assert '🍅 91%' in card
    dumped = json.dumps({'response': card}, ensure_ascii=False)
    assert json.loads(dumped)['response'] == card


def test_telegram_bot_uses_shared_card_formatter():
    """Карточка «Подробнее» собирается общей функцией — дубля формата нет."""
    import telegram_bot

    assert telegram_bot.format_movie_card is format_movie_card


# --- Прямой тест callback «Подробнее» (minor ревью B7, закрывает B8) ---


# Полный документ фильма kinopoisk.dev, каким его получает handle_movie_detail
_KP_DETAIL_PAYLOAD = {
    'id': 447301,
    'name': 'Начало',
    'year': 2010,
    'genres': [{'name': 'фантастика'}],
    'countries': [{'name': 'США'}],
    'rating': {'imdb': 8.8, 'kp': 8.7},
    'poster': {'url': ''},  # пустой URL — reply_photo не вызывается
    'description': 'Сон внутри сна',
    'externalId': {'imdb': 'tt1375666'},
}


class _FakeDetailResponse:
    """Ответ фейковой HTTP-сессии на GET (контекст-менеджер aiohttp)."""

    def __init__(self, payload: Any, status: int = 200):
        self.status = status
        self._payload = payload

    async def json(self) -> Any:
        return self._payload

    async def __aenter__(self) -> '_FakeDetailResponse':
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


class _FakeDetailSession:
    """Фейковая aiohttp-сессия: один и тот же документ на любой GET."""

    def __init__(self, payload: Any = _KP_DETAIL_PAYLOAD):
        self.payload = payload
        self.get_calls: List[Tuple[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeDetailResponse:
        self.get_calls.append((url, kwargs))
        return _FakeDetailResponse(self.payload)

    async def __aenter__(self) -> '_FakeDetailSession':
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


class _FakeCallbackMessage:
    """Сообщение, к которому привязан callback: записывает ответы."""

    def __init__(self):
        self.texts: List[Tuple[str, Any]] = []
        self.photos: List[Any] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.texts.append((text, kwargs))

    async def reply_photo(self, photo: Any, **kwargs: Any) -> None:
        self.photos.append((photo, kwargs))


class _FakeCallbackQuery:
    def __init__(self, data: str):
        self.data = data
        self.answer_calls = 0
        self.message = _FakeCallbackMessage()

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answer_calls += 1


class _FakeCallbackUpdate:
    def __init__(self, data: str):
        self.callback_query = _FakeCallbackQuery(data)


def _run_detail(monkeypatch, data: str = 'info:447301') -> _FakeCallbackQuery:
    """Вызвать handle_movie_detail на фейках; вернуть query с записью ответов."""
    import telegram_bot

    monkeypatch.setattr(
        telegram_bot.aiohttp, 'ClientSession',
        lambda *a, **kw: _FakeDetailSession(),
    )
    update = _FakeCallbackUpdate(data)
    asyncio.run(telegram_bot.handle_movie_detail(update, None))
    return update.callback_query


def test_movie_detail_card_contains_badge(monkeypatch):
    """Прямой сценарий «Подробнее»: карточка содержит бейдж «🍅 91%».

    Kinopoisk-ответ и query замокированы; обогащение имитирует результат
    B5 (rt_score=91) — проверяется, что общий форматтер карточки (B7)
    доводит бейдж до пользователя с parse_mode='HTML'.
    """
    import telegram_bot

    async def _fake_enrich(session, movies, client=None):
        for movie in movies:
            movie['rt_score'] = 91
            movie['metascore'] = 82
        return movies

    monkeypatch.setattr(
        telegram_bot, 'enrich_movies_with_rt_scores', _fake_enrich
    )
    query = _run_detail(monkeypatch)

    assert query.answer_calls == 1
    assert len(query.message.texts) == 1
    text, kwargs = query.message.texts[0]
    assert '🍅 91%' in text
    # Карточка собрана общей функцией форматирования (B7)
    assert text == format_movie_card({
        'id': 447301, 'title': 'Начало', 'year': 2010,
        'genre': 'фантастика', 'country': 'США',
        'rating': 8.8, 'rating_imdb': 8.8, 'rating_kp': 8.7,
        'description': 'Сон внутри сна', 'poster_url': '',
        'imdb_id': 'tt1375666', 'rt_score': 91, 'metascore': 82,
    })
    assert kwargs.get('parse_mode') == 'HTML'
    # poster пустой — фото не отправляется
    assert query.message.photos == []


def test_movie_detail_flag_off_is_phase0(monkeypatch):
    """Callback «Подробнее» при flag off — поведение фазы 0: без бейджа/сети.

    Вызывается НАСТОЯЩАЯ enrich_movies_with_rt_scores: при выключенном
    флаге она проставляет rt_score=None без создания клиента OMDb
    («бомба» _make_client не срабатывает) — карточка без «🍅».
    """
    import rt_enrichment

    monkeypatch.setenv('ENABLE_RT_SCORES', 'false')
    # След вызова фиксируется ДО raise: AssertionError проглатывается
    # контуром fail-silent, доказательством служит пустой список creations
    creations: List[str] = []

    def _sentinel_make_client():
        creations.append('make_client')
        raise AssertionError('клиент OMDb создан при выключенном флаге')

    monkeypatch.setattr(rt_enrichment, '_make_client', _sentinel_make_client)
    query = _run_detail(monkeypatch)

    assert creations == []
    assert len(query.message.texts) == 1
    text, _ = query.message.texts[0]
    assert '🍅' not in text
    assert 'Начало' in text and 'рейтингом 8.8' in text
