"""Общая конфигурация pytest и разделяемые фейки тестов kinobot.

Здесь живут: подготовка sys.path/окружения, общие классы-фейки PTB
(Message/Chat/User/CallbackQuery/Update) и aiohttp, заглушка менеджера
сессий, фабрики фильма/менеджера диалога и хелперы проверки HTML
(сбалансированность тегов, битые сущности), запуск корутин в синхронных
тестах (`run_coro`) и фабрики фильмов списка (`make_list_movie`,
`make_list_movies`). Вынесено из дублей тестовых
файлов фазы 0 UI/UX (nit n4 ревью A7+A5, изменение
verify-phase0-uiux-tests): определение каждого фейка — в одном месте,
тестовые модули импортируют их отсюда (`from conftest import ...`
работает благодаря режиму `--import-mode=prepend`, явно зафиксированному
в `pyproject.toml` (addopts): каталог tests/ добавляется в sys.path).
"""
import asyncio
import os
import re
import sys
from html.parser import HTMLParser
from typing import Any, List, Optional, Tuple
from unittest.mock import AsyncMock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# Тестовые значения ключей: без них конструкторы клиентов не создать,
# сетевых вызовов в тестах нет
os.environ.setdefault('GIGACHAT_AUTH_KEY', 'test-gigachat-key')
os.environ.setdefault('KINOPOISK_API_KEY', 'test-kinopoisk-key')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-telegram-token')

# Импорты src/ и PTB — ПОСЛЕ настройки sys.path и окружения (E402 для этого
# файла отключён per-file-ignore в pyproject.toml, как и для src/telegram_bot.py).
# `telegram_bot` намеренно НЕ импортируется здесь: он тянет весь стек бота
# (load_dotenv, setup_logging, SessionManager, DialogueManager) в ЛЮБОЙ прогон
# pytest — единственный потребитель (`install_callback_mocks`) импортирует его
# внутри функции (модуль к тому моменту уже в sys.modules у тестов фазы 0).
from dialogue_manager import DialogueManager, build_movie_card
from session_manager import UserSession
from telegram import InlineKeyboardMarkup, Message

# --- Общие тестовые данные фазы 0 ---

POSTER_URL = 'https://example.com/poster.jpg'

# Полный документ фильма kinopoisk.dev, каким его получает callback info:
KP_PAYLOAD = {
    'id': 447301,
    'name': 'Начало',
    'year': 2010,
    'genres': [{'name': 'фантастика'}],
    'countries': [{'name': 'США'}],
    'rating': {'imdb': 8.8, 'kp': 8.7},
    'poster': {'url': POSTER_URL},
    'description': 'Сон внутри сна',
    'externalId': {'imdb': 'tt1375666'},
}

# Описание длиннее бюджета caption: с HTML-символами и сущностями
LONG_DESCRIPTION = 'Кадм & Хараппа <b>важно</b> "цитата" ' * 400

# Незакрытая сущность: «&», после которого нет корректного «…;»
BROKEN_ENTITY_RE = re.compile(r'&(?![a-zA-Z#][a-zA-Z0-9]{1,8};)')


# --- Заглушки и фейки PTB/aiohttp (паттерн test_rt_badge_compat) ---


class StubSessionManager:
    """In-memory заглушка менеджера сессий (без обращений к PostgreSQL).

    Суперсет версий A4/A5: журнал `saved` фиксирует вызовы save_session —
    тестам, которым он не нужен, поле просто не мешает.
    """

    def __init__(self):
        self.sessions = {}
        self.saved: List[Any] = []

    def get_session(self, user_id):
        if user_id not in self.sessions:
            self.sessions[user_id] = UserSession(user_id)
        return self.sessions[user_id]

    def save_session(self, session):
        self.saved.append(session.user_id)

    def clear_session(self, user_id):
        self.sessions.pop(user_id, None)


class FakeChat:
    """Чат сообщения: записывает статусы «печатает…»."""

    def __init__(self):
        self.actions: List[Any] = []

    async def send_action(self, **kwargs: Any) -> None:
        self.actions.append(kwargs)


class FakeMessage:
    """Сообщение БЕЗ edit-методов: записывает ответы, умеет имитировать отказ.

    Gate `_edit_or_send` (A5) направляет доставку такого объекта прежним
    путём (`_send_result`); `fail_photo=True` имитирует отказ Telegram на
    URL постера (fallback карточки на текст, A4).
    """

    def __init__(self, chat: Optional[FakeChat] = None, fail_photo: bool = False):
        self.chat = chat
        self.fail_photo = fail_photo
        self.texts: List[Tuple[str, Any]] = []
        self.photos: List[Tuple[Any, Any]] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.texts.append((text, kwargs))

    async def reply_photo(self, photo: Any, **kwargs: Any) -> None:
        if self.fail_photo:
            raise RuntimeError('Telegram отклонил URL постера')
        self.photos.append((photo, kwargs))


class FakeUser:
    def __init__(self, user_id: int = 777):
        self.id = user_id


class FakeQuery:
    def __init__(self, data: str, message: Any = None, from_user: Any = None):
        self.data = data
        self.message = message if message is not None else FakeMessage()
        self.from_user = from_user
        self.answer_calls = 0

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answer_calls += 1


class FakeCallbackUpdate:
    """Update С callback-запросом (без `.message` — по образцу test_rt_badge_compat).

    Имя явно отделяет фейк от message-update (у `test_bot_commands.py` свой
    `_FakeUpdate` с `.message`): подмена нужна, чтобы не смешивать контуры.
    """

    def __init__(self, data: str, message: Any = None, from_user: Any = None):
        self.callback_query = FakeQuery(data, message, from_user)
        self.effective_user = from_user


class FakeResponse:
    """Ответ фейковой HTTP-сессии на GET (контекст-менеджер aiohttp)."""

    def __init__(self, payload: Any, status: int = 200):
        self.status = status
        self._payload = payload

    async def json(self) -> Any:
        return self._payload

    async def __aenter__(self) -> 'FakeResponse':
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


class FakeHttpSession:
    """Фейковая aiohttp-сессия: один и тот же документ на любой GET."""

    def __init__(self, payload: Any = None, status: int = 200):
        self.payload = payload
        self.status = status

    async def __aenter__(self) -> 'FakeHttpSession':
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return FakeResponse(self.payload, self.status)


# --- Фабрики и хелперы ---


def make_movie(**overrides: Any) -> dict:
    """Фильм финальной выдачи с полным набором полей (базовый словарь A4/A5).

    Файлы, чей локальный `_movie` имел иной набор полей (A2/A3/A7), держат
    обёртку над этой фабрикой — наборы участвуют в побайтовых assert'ах
    формата (design.md D2 изменения verify-phase0-uiux-tests).
    """
    movie = {
        'id': 435, 'title': 'Дюна', 'year': 2021,
        'genre': 'фантастика', 'country': 'США',
        'rating': 7.8, 'rating_source': 'IMDB',
        'description': 'Короткое описание',
        'kinopoisk_url': 'https://www.kinopoisk.ru/film/435/',
        'poster_url': POSTER_URL,
    }
    movie.update(overrides)
    return movie


def make_list_movie(**overrides: Any) -> dict:
    """Фильм списка выдачи: без description/poster_url (список их не использует).

    Общий хелпер B2/B3 (ревью n2, изменение add-list-nav-and-pagination):
    идентичные локальные `_movie` тестовых модулей пагинации и quick replies
    вынесены сюда — определение в одном месте.
    """
    movie = make_movie()
    movie.pop('description', None)
    movie.pop('poster_url', None)
    movie.update(overrides)
    return movie


def make_list_movies(count: int, title_prefix: str = 'Фильм') -> List[dict]:
    """Выдача из `count` фильмов списка: id 1..count, заголовки «{prefix} N»."""
    return [make_list_movie(id=i, title=f'{title_prefix} {i}') for i in range(1, count + 1)]


def run_coro(coro: Any) -> Any:
    """Прогнать корутину в синхронном тесте (тонкая обёртка `asyncio.run`)."""
    return asyncio.run(coro)


def make_manager() -> DialogueManager:
    """DialogueManager с in-memory заглушкой менеджера сессий."""
    return DialogueManager(StubSessionManager())


def install_callback_mocks(monkeypatch: pytest.MonkeyPatch, dm: DialogueManager, payload: Any = None, status: int = 200) -> None:
    """Подменить менеджера диалога, aiohttp и RT-обогащение для callback-тестов.

    `telegram_bot` импортируется ЗДЕСЬ, а не на уровне модуля conftest:
    глобальный импорт тянул бы весь стек бота (load_dotenv, setup_logging,
    SessionManager, DialogueManager) в любой прогон pytest, и сбой импорта
    бота валил бы все тесты (minor mn1 ревью R1).
    """
    import telegram_bot

    if payload is None:
        payload = KP_PAYLOAD
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    monkeypatch.setattr(telegram_bot.aiohttp, 'ClientSession', lambda *a, **kw: FakeHttpSession(payload, status))
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda *a, **kw: None)

    async def _identity_enrich(session, movies, client=None):
        # Имитация результата B5: те же словари плюс RT-оценки
        for movie in movies:
            movie['rt_score'] = 91
            movie['metascore'] = 82
        return movies

    monkeypatch.setattr(telegram_bot, 'enrich_movies_with_rt_scores', _identity_enrich)


def editable_message(photo: Tuple[Any, ...] = ()) -> AsyncMock:
    """Редактируемое PTB-сообщение: проходит gate, edit/delete/reply — awaitable."""
    message = AsyncMock(spec=Message)
    message.photo = photo
    message.text = 'старый текст'
    message.chat = AsyncMock()  # статус «печатает…» не должен падать
    return message


def recording_mock(calls: List[str], name: str) -> AsyncMock:
    """AsyncMock, пишущий имя вызова в общий список — для проверки ПОРЯДКА."""

    async def _side_effect(*args: Any, **kwargs: Any) -> None:
        calls.append(name)

    return AsyncMock(side_effect=_side_effect)


def all_buttons(keyboard: InlineKeyboardMarkup) -> List[Any]:
    """Все inline-кнопки клавиатуры (по рядам)."""
    return [b for row in keyboard.inline_keyboard for b in row]


def number_buttons(keyboard: InlineKeyboardMarkup) -> List[Any]:
    """Номерные кнопки клавиатуры списка (все ряды до навигационного)."""
    return [b for row in keyboard.inline_keyboard[:-1] for b in row]


class TagBalanceChecker(HTMLParser):
    """Проверяет сбалансированность HTML-тегов в тексте карточки."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: List[str] = []
        self.errors: List[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(tag)
        else:
            self.stack.pop()


def assert_html_balanced(text: str) -> None:
    """Утверждение: HTML-теги текста сбалансированы (нет незакрытых/лишних)."""
    checker = TagBalanceChecker()
    checker.feed(text)
    checker.close()
    assert not checker.errors, f'несбалансированные закрывающие теги: {checker.errors}'
    assert not checker.stack, f'незакрытые теги: {checker.stack}'


def make_card_result(poster: str = POSTER_URL) -> dict:
    """Результат-карточка (`build_movie_card`) для тестов `_edit_or_send`."""
    movie = make_movie(poster_url=poster)
    text, markup = build_movie_card(movie)
    return {'response': text, 'reply_markup': markup, 'movie': movie}


def make_list_result() -> dict:
    """Результат-список (текст без постера) для тестов `_edit_or_send`."""
    return {'response': '<strong>Список:</strong>\n1. Фильм\n', 'reply_markup': None}
