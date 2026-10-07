# -*- coding: utf-8 -*-
"""Тесты T4 (сценарий 3): смоук-таблица маршрутов `_CALLBACK_ROUTES`.

Изменение add-db-callback-regression-tests-t4 (задача T4 бэклога
backlog_2026-10-04_bugfix_telegram_ui.md). Защита от «выпавших» маршрутов
в будущем:

- структурные инварианты таблицы: у каждого префикса есть хендлер
  (callable, не None), префиксы уникальны и ни один не является
  `startswith`-префиксом другого (детерминированность диспетчера);
- параметризованный позитивный dispatch по ВСЕМ префиксам ИЗ таблицы
  (второй хардкод-список запрещён условием задачи): репрезентативный
  `callback_data` каждого маршрута проходит через `handle_movie_detail`
  с моками — `answer()` вызван, ответ доставлен, исключений нет, текст
  «неизвестный callback» и классифицированные тексты ошибок НЕ показаны;
- словарь сценариев `SAMPLE_SCENARIOS` покрывает таблицу РОВНО (новый
  маршрут без позитивного сценария или сценарий-сирота роняют прогон).

Все проверки офлайн: фейки PTB/aiohttp из tests/conftest.py, подменные
менеджеры watchlist и in-memory сессии; реальная БД не нужна даже для
`fb:`/`menu:`/`top:` — репрезентативные сценарии выбраны без хранилища
(`fb:rate:{id}` — чистый рендер панели, `menu:main`/`top:menu` — рендер
меню). БД-маршруты (`save:`/`unsave:`/`watchlist:`/`wpage:`) идут через
подменный watchlist-менеджер (паттерн test_menu_callbacks_b8).
"""
from typing import Any, Callable, Dict, List
from unittest.mock import AsyncMock

import pytest

import telegram_bot
from conftest import (
    KP_PAYLOAD,
    FakeCallbackUpdate,
    FakeChat,
    FakeMessage,
    FakeUser,
    install_callback_mocks,
    make_card_result,
    make_list_movies,
    make_manager,
    make_movie,
    run_coro,
)
from dialogue_manager import (
    FEEDBACK_CALLBACK_PREFIX,
    PAGE_CALLBACK_PREFIX,
    RANDOM_MOVIE_CALLBACK,
    SAVE_CALLBACK_PREFIX,
    UNSAVE_CALLBACK_PREFIX,
    WATCHLIST_CALLBACK,
    WATCHLIST_PAGE_PREFIX,
    compute_list_hash,
)

USER_ID = 777

# Префиксы берутся ИЗ таблицы маршрутов прод-кода — единственный источник
# параметризации (условие задачи T4, design D4).
ROUTE_PREFIXES: List[str] = [prefix for prefix, _ in telegram_bot._CALLBACK_ROUTES]


# --- Заглушка watchlist-менеджера (паттерн _FakeWatchlist из B8) ---


class FakeWatchlistManager:
    """In-memory заглушка WatchlistManager: страница списка, добавление/удаление."""

    def __init__(self, items: List[dict] | None = None) -> None:
        self.items = list(items or [])
        self.total = len(self.items)
        self.added: List[Any] = []
        self.removed: List[Any] = []

    def list_page(self, user_id, offset, limit):
        return self.items[offset:offset + limit], self.total

    def add(self, user_id, **kwargs):
        self.added.append((user_id, kwargs))
        return True

    def contains(self, user_id, kinopoisk_id):
        return False

    def remove(self, user_id, kinopoisk_id):
        self.removed.append((user_id, kinopoisk_id))
        return True


def _watchlist_items(count: int) -> List[dict]:
    """Элементы сохранённого списка (набор полей — образец B8)."""
    return [
        {'kinopoisk_id': i, 'title': f'Фильм {i}', 'year': 2000 + i, 'poster_url': None, 'added_at': None}
        for i in range(1, count + 1)
    ]


# --- Репрезентативные позитивные сценарии маршрутов (design D4) ---
# Каждая функция ставит моки своего маршрута и возвращает callback_data,
# начинающийся с соответствующего префикса таблицы.


def _setup_info(monkeypatch: pytest.MonkeyPatch) -> str:
    """Карточка фильма: фейковый HTTP отдаёт полный документ Kinopoisk."""
    install_callback_mocks(monkeypatch, make_manager())
    return f'info:{KP_PAYLOAD["id"]}'


def _setup_alt(monkeypatch: pytest.MonkeyPatch) -> str:
    """«Другие варианты»: LLM-пайплайн подменён готовым списком."""
    dm = make_manager()
    dm.process_message = AsyncMock(return_value={'response': 'Другие варианты:', 'reply_markup': None})  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)
    return 'alt:list'


def _setup_similar(monkeypatch: pytest.MonkeyPatch) -> str:
    """«Похожие»: similar-пайплайн подменён готовым результатом."""
    dm = make_manager()
    dm.find_similar_by_id = AsyncMock(return_value={'response': 'Похожие фильмы:', 'reply_markup': None})  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)
    return 'similar:435'


def _setup_back(monkeypatch: pytest.MonkeyPatch) -> str:
    """«К списку»: in-memory сессия с сохранённой выдачей."""
    dm = make_manager()
    dm.session_manager.get_session(str(USER_ID)).last_movies = make_list_movies(3)  # type: ignore[attr-defined]
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    return 'back:list'


def _setup_random(monkeypatch: pytest.MonkeyPatch) -> str:
    """«Случайный фильм»: движок подменён готовой карточкой."""
    dm = make_manager()
    dm.get_random_movie = AsyncMock(return_value=make_card_result())  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)
    return RANDOM_MOVIE_CALLBACK


def _setup_mood(monkeypatch: pytest.MonkeyPatch) -> str:
    """«По настроению»: чистый рендер приглашения (без LLM и БД)."""
    install_callback_mocks(monkeypatch, make_manager())
    return telegram_bot.MOOD_START_CALLBACK


def _setup_retry(monkeypatch: pytest.MonkeyPatch) -> str:
    """«Повторить»: цель mood делегирует маршруту приглашения (без сети)."""
    install_callback_mocks(monkeypatch, make_manager())
    return 'retry:mood'


def _setup_page(monkeypatch: pytest.MonkeyPatch) -> str:
    """Страница выдачи: сессия с 7 фильмами и актуальным hash списка."""
    dm = make_manager()
    movies = make_list_movies(7)
    dm.session_manager.get_session(str(USER_ID)).last_movies = movies  # type: ignore[attr-defined]
    install_callback_mocks(monkeypatch, dm)
    return f'{PAGE_CALLBACK_PREFIX}5:{compute_list_hash(movies)}'


def _setup_save(monkeypatch: pytest.MonkeyPatch) -> str:
    """«Сохранить»: фильм в сессии, watchlist-менеджер подменён."""
    dm = make_manager()
    dm.session_manager.get_session(str(USER_ID)).last_movies = [make_movie(id=435)]  # type: ignore[attr-defined]
    install_callback_mocks(monkeypatch, dm)
    fake = FakeWatchlistManager()
    monkeypatch.setattr(telegram_bot, 'get_watchlist_manager', lambda: fake)
    return f'{SAVE_CALLBACK_PREFIX}435'


def _setup_unsave(monkeypatch: pytest.MonkeyPatch) -> str:
    """«Удалить»: watchlist-менеджер подменён, список перерисовывается."""
    install_callback_mocks(monkeypatch, make_manager())
    fake = FakeWatchlistManager(items=_watchlist_items(1))
    monkeypatch.setattr(telegram_bot, 'get_watchlist_manager', lambda: fake)
    return f'{UNSAVE_CALLBACK_PREFIX}435'


def _setup_watchlist(monkeypatch: pytest.MonkeyPatch) -> str:
    """«Мой список»: первая страница подменённого менеджера."""
    install_callback_mocks(monkeypatch, make_manager())
    fake = FakeWatchlistManager(items=_watchlist_items(2))
    monkeypatch.setattr(telegram_bot, 'get_watchlist_manager', lambda: fake)
    return WATCHLIST_CALLBACK


def _setup_wpage(monkeypatch: pytest.MonkeyPatch) -> str:
    """Страница списка: 7 элементов, offset 5 даёт непустой срез."""
    install_callback_mocks(monkeypatch, make_manager())
    fake = FakeWatchlistManager(items=_watchlist_items(7))
    monkeypatch.setattr(telegram_bot, 'get_watchlist_manager', lambda: fake)
    return f'{WATCHLIST_PAGE_PREFIX}5'


def _setup_fb(monkeypatch: pytest.MonkeyPatch) -> str:
    """Панель оценки фидбека: чистый рендер БЕЗ БД (design B5 D3)."""
    return f'{FEEDBACK_CALLBACK_PREFIX}rate:435'


def _setup_menu(monkeypatch: pytest.MonkeyPatch) -> str:
    """Действие меню: возврат к главному меню (чистый рендер)."""
    return f'{telegram_bot._MENU_PREFIX}main'


def _setup_top(monkeypatch: pytest.MonkeyPatch) -> str:
    """Подменю топов: возврат к главному меню (чистый рендер)."""
    return f'{telegram_bot._TOP_PREFIX}menu'


def _setup_genre(monkeypatch: pytest.MonkeyPatch) -> str:
    """Меню жанров (T6): возврат к главному меню — чистый рендер без БД и LLM."""
    return telegram_bot._GENRE_MENU_SEGMENT


# Ключи — префиксы таблицы маршрутов: из констант прод-кода там, где они
# объявлены, и литералы таблицы для прочих (единственный источник — таблица;
# покрытие проверяется test_sample_scenarios_cover_route_table_exactly).
SAMPLE_SCENARIOS: Dict[str, Callable[[pytest.MonkeyPatch], str]] = {
    'info:': _setup_info,
    'alt:': _setup_alt,
    'similar:': _setup_similar,
    'back:': _setup_back,
    'random:': _setup_random,
    'mood:': _setup_mood,
    'retry:': _setup_retry,
    PAGE_CALLBACK_PREFIX: _setup_page,
    SAVE_CALLBACK_PREFIX: _setup_save,
    UNSAVE_CALLBACK_PREFIX: _setup_unsave,
    'watchlist:': _setup_watchlist,
    WATCHLIST_PAGE_PREFIX: _setup_wpage,
    FEEDBACK_CALLBACK_PREFIX: _setup_fb,
    telegram_bot._MENU_PREFIX: _setup_menu,
    telegram_bot._TOP_PREFIX: _setup_top,
    telegram_bot._GENRE_PREFIX: _setup_genre,
}

# Тексты, которых НЕ должно быть в позитивном ответе маршрута.
FORBIDDEN_ERROR_TEXTS = (
    telegram_bot._ERROR_TEXT_UNKNOWN_CALLBACK,
    telegram_bot._ERROR_TEXT_GENERIC,
    telegram_bot._ERROR_TEXT_NETWORK,
    telegram_bot._ERROR_TEXT_LLM,
)


# === 1. Структурные инварианты таблицы маршрутов (п.2.1) ===


def test_every_route_has_callable_handler():
    """У каждого префикса таблицы есть хендлер — callable, не None."""
    assert telegram_bot._CALLBACK_ROUTES, 'таблица маршрутов пуста'
    for prefix, handler in telegram_bot._CALLBACK_ROUTES:
        assert isinstance(prefix, str) and prefix, f'некорректный префикс: {prefix!r}'
        assert handler is not None, f'у префикса {prefix!r} нет хендлера'
        assert callable(handler), f'хендлер префикса {prefix!r} не вызываемый'


def test_prefixes_unique_and_without_startswith_collisions():
    """Префиксы уникальны и ни один не startswith-префикс другого.

    Диспетчер обходит таблицу по порядку и берёт ПЕРВОЕ совпадение
    `data.startswith(prefix)`: коллизия сделала бы маршрутизацию
    двусмысленной (порядок-зависимой).
    """
    assert len(ROUTE_PREFIXES) == len(set(ROUTE_PREFIXES)), 'в таблице дубли префиксов'
    for a in ROUTE_PREFIXES:
        for b in ROUTE_PREFIXES:
            if a != b:
                assert not a.startswith(b), f'префикс {a!r} двусмысленно коллидирует с {b!r}'


# === 2. Покрытие таблицы сценариями (п.2.4) ===


def test_sample_scenarios_cover_route_table_exactly():
    """Сценарии покрывают таблицу РОВНО: без пропусков и сирот.

    Новый маршрут без позитивного сценария (или сценарий удалённого
    маршрута) роняет этот тест — защита от «выпавших» маршрутов (T4 п.3).
    """
    route_set = set(ROUTE_PREFIXES)
    sample_set = set(SAMPLE_SCENARIOS)
    assert route_set - sample_set == set(), f'маршруты без позитивного сценария: {sorted(route_set - sample_set)}'
    assert sample_set - route_set == set(), f'сценарии без маршрута в таблице: {sorted(sample_set - route_set)}'


# === 3. Позитивный dispatch каждого маршрута (п.2.2/2.3) ===


@pytest.mark.parametrize('prefix', ROUTE_PREFIXES)
def test_route_positive_dispatch(prefix: str, monkeypatch: pytest.MonkeyPatch):
    """Репрезентативный callback маршрута: answer, доставка, без ошибок.

    Позитивность (design D4): `query.answer()` вызван ровно один раз
    (индикация нажатия снята), обработчик отработал без исключения
    (не падение run_coro), ответ доставлен (текст ИЛИ фото с подписью),
    «неизвестный callback» и классифицированные тексты ошибок не показаны.
    """
    setup = SAMPLE_SCENARIOS.get(prefix)
    assert setup is not None, f'нет позитивного сценария для маршрута {prefix!r}'
    data = setup(monkeypatch)
    assert data.startswith(prefix), f'сценарий {prefix!r} вернул чужой callback_data: {data!r}'

    msg = FakeMessage(chat=FakeChat())
    update = FakeCallbackUpdate(data, message=msg, from_user=FakeUser(USER_ID))
    run_coro(telegram_bot.handle_movie_detail(update, None))

    query = update.callback_query
    assert query.answer_calls == 1, f'маршрут {prefix!r} не снял индикацию нажатия'
    delivered = [text for text, _ in msg.texts]
    delivered += [str(kwargs.get('caption') or '') for _, kwargs in msg.photos]
    assert delivered, f'маршрут {prefix!r} ничего не доставил'
    for text in delivered:
        assert text, f'маршрут {prefix!r} доставил пустой текст'
        for forbidden in FORBIDDEN_ERROR_TEXTS:
            assert forbidden not in text, f'маршрут {prefix!r} показал текст ошибки: {forbidden!r}'
