"""Тесты B9: сквозные приёмочные проверки фазы 1 (verify-phase1-regression).

Без сети и реальной БД: проверяются КРИТЕРИИ ПРИЁМКИ задачи B9 целиком, а не
точечное поведение задач B1–B8 (оно покрыто их собственными тестами —
test_onboarding_b1, test_quick_replies_b2, test_pagination_b3,
test_watchlist_b4, test_feedback_b5, test_emoji_guideline,
test_error_recovery_b7, test_menu_callbacks_b8). Gap-анализ чек-листа B9
(раздел 1 tasks.md изменения) выявил гэпы, закрытые ЭТИМ файлом:

1. истечение кэша выдачи `MovieAgent._search_cache` по TTL (`CACHE_TTL`,
   по умолчанию 45 с): hit в пределах TTL и miss после состаривания записи;
2. watchlist и фидбек ПЕРЕЖИВАЮТ «рестарт» процесса: мок PostgreSQL —
   SQLite-файл (tmp_path), данные читает НОВЫЙ экземпляр менеджера и новое
   минимальное Flask-приложение; дубль отклоняется, upsert фидбека не плодит
   строки через «рестарт»;
3. сквозная диспетчеризация callback по ВСЕМ префиксам фазы 1: состав и
   порядок таблицы `_CALLBACK_ROUTES`, каждый префикс → свой обработчик,
   `answer()` на любой callback, неизвестный префикс → дружелюбный выход
   с кнопками (B7, без dead-end);
4. онбординг-кнопка `random:movie`: состав клавиатуры `/start` и привязка
   префикса `random:` к обработчику случайного фильма;
 5. единственный источник схем новых таблиц (watchlist, movie_feedback):
    `init_db.py` не дублирует определения и накатывает схему миграциями
    (тонкая обёртка T14: единственный источник DDL — migrations/, ловушка
    AGENTS.md «схемы идентичны» обеспечивается конструкцией);
6. ошибки: представитель каждого класса (network/llm/generic) → СВОЙ текст
   и ≥1 кнопка-действие (приёмочная сводка поверх точечного покрытия B7);
7. nit n5 (handoff B8): десятилетие примера в справке `/help` вычисляется
   из `config.CURRENT_YEAR`, хардкода «2020-х» в исходнике нет.

Регрессии фазы 0 (A8) проверяются прогоном `tests/test_phase0_verification_a8.py`
в общем наборе pytest — этот файл их не дублирует.
"""
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock

import aiohttp
import pytest
from flask import Flask

import models.database as db_module
import telegram_bot
from conftest import (
    POSTER_URL,
    FakeCallbackUpdate,
    FakeChat,
    FakeMessage,
    FakeUser,
    all_buttons,
    make_list_movie,
    make_list_movies,
    make_manager,
    run_coro,
)
from dialogue_manager import (
    LIST_END_TEXT,
    PAGE_CALLBACK_PREFIX,
    RANDOM_MOVIE_CALLBACK,
    SAVE_CALLBACK_PREFIX,
    UNSAVE_CALLBACK_PREFIX,
    WATCHLIST_PAGE_PREFIX,
    FEEDBACK_CALLBACK_PREFIX,
    compute_list_hash,
)
from feedback_manager import FeedbackManager
from models.database import MovieFeedback, Watchlist
from movie_agent import MovieAgent
from watchlist_manager import WatchlistManager

ROOT = Path(__file__).resolve().parents[1]


# === 1. Пагинация: приёмочная сводка (offset / последняя страница) ===


def test_pagination_second_page_and_last_page():
    """Список из 12 фильмов: страница offset=5 и последняя страница offset=10.

    Приёмочный уровень B3: нумерация продолжается с offset, кнопка «⬇️ Ещё 5»
    (`page:{offset+limit}:{hash}`) есть только пока осталось продолжение,
    на последней странице её нет и добавлен текст-завершение LIST_END_TEXT.
    """
    dm = make_manager()
    movies = make_list_movies(12)

    # Вторая страница (offset=5): строки 6–10, есть продолжение
    page2, kb2 = dm.render_movie_list(movies, 'Заголовок', offset=5)
    lines2 = page2.splitlines()
    assert lines2[1].startswith('6.')
    assert lines2[5].startswith('10.')
    assert LIST_END_TEXT not in page2
    more2 = [b for b in kb2.inline_keyboard[-2] if (b.callback_data or '').startswith(PAGE_CALLBACK_PREFIX)]
    assert len(more2) == 1
    assert more2[0].callback_data == f'{PAGE_CALLBACK_PREFIX}10:{compute_list_hash(movies)}'

    # Последняя страница (offset=10): строки 11–12, продолжения нет
    last, kb_last = dm.render_movie_list(movies, 'Заголовок', offset=10)
    lines_last = last.splitlines()
    assert lines_last[1].startswith('11.')
    assert lines_last[2].startswith('12.')
    assert lines_last[-1] == LIST_END_TEXT
    more_last = [b for b in kb_last.inline_keyboard[-2] if (b.callback_data or '').startswith(PAGE_CALLBACK_PREFIX)]
    assert more_last == []


# === 2. Истечение кэша выдачи по TTL (чек-лист B9, гэп 1) ===


def test_search_cache_hit_within_ttl_and_miss_after_expiry(monkeypatch):
    """Кэш `MovieAgent._search_cache`: hit до TTL, miss после истечения.

    TTL фиксируется monkeypatch'ем (`CACHE_TTL=45` — значение по умолчанию
    config), чтобы тест не зависел от переменной окружения. Запись старше
    TTL удаляется, движок рекомендаций вызывается повторно, кэш обновляется
    свежей меткой времени (src/movie_agent.py: ветка else у `CACHE_TTL`).
    """
    import movie_agent

    monkeypatch.setattr(movie_agent, 'CACHE_TTL', 45)
    agent = MovieAgent()
    first = [make_list_movie(id=1, title='Первый')]
    second = [make_list_movie(id=2, title='Второй')]
    engine = SimpleNamespace(get_recommendations=AsyncMock(side_effect=[first, second]))
    agent.recommendation_engine = engine

    kwargs: Dict[str, Any] = {'user_id': '777', 'query': 'комедия'}
    assert run_coro(agent.recommend_movies(None, **kwargs)) == first
    assert engine.get_recommendations.await_count == 1

    # Повтор в пределах TTL — попадание в кэш: движок НЕ вызывается
    assert run_coro(agent.recommend_movies(None, **kwargs)) == first
    assert engine.get_recommendations.await_count == 1

    # Состариваем запись за пределы TTL (>45 с) — промах: повторный вызов
    # движка, свежая выдача и обновление метки времени в кэше
    key = next(iter(agent._search_cache))
    data, timestamp = agent._search_cache[key]
    agent._search_cache[key] = (data, timestamp - 46)
    assert run_coro(agent.recommend_movies(None, **kwargs)) == second
    assert engine.get_recommendations.await_count == 2
    fresh_timestamp = agent._search_cache[key][1]
    assert time.time() - fresh_timestamp < 45


# === 3. Watchlist/фидбек переживают «рестарт» (мок PG, гэп 2) ===


@pytest.fixture()
def pg_boot(tmp_path):
    """Мок PostgreSQL: SQLite-ФАЙЛ, общий для нескольких «запусков» процесса.

    Каждый вызов `pg_boot(name)` создаёт НОВОЕ минимальное Flask-приложение
    (имитация рестарта воркера) поверх того же файла БД — паттерн
    `sqlite_app` из test_watchlist_b4/test_feedback_b5, расширенный на
    несколько приложений: in-memory `sqlite://` не подходит, данные должны
    переживать смену экземпляра менеджера (критерий B9 «переживает рестарт»).
    """
    db = db_module.db
    uri = f"sqlite:///{(tmp_path / 'kinobot_b9.db').as_posix()}"
    apps: List[Flask] = []

    def _boot(name: str) -> Flask:
        app = Flask(name)
        app.config['SQLALCHEMY_DATABASE_URI'] = uri
        app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(app)
        apps.append(app)
        with app.app_context():
            # Идемпотентно (checkfirst): второй «запуск» таблицы не пересоздаёт
            db.create_all()
        return app

    yield _boot
    for app in apps:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()


def test_watchlist_survives_restart_duplicate_and_remove(pg_boot):
    """Сохранение → дубль отклонён → «рестарт» → данные на месте → удаление.

    Полный цикл чек-листа B9 для watchlist на моке PG: персистентность
    гарантируется БД (а не in-memory), поэтому НОВЫЙ экземпляр
    `WatchlistManager` (новое Flask-приложение на том же файле) видит
    сохранённый фильм; дубль пары (user_id, kinopoisk_id) отклоняется на
    уровне схемы; удаление видно любому последующему экземпляру.
    """
    manager_before = WatchlistManager(app=pg_boot('kinobot_b9_wl_first'))
    assert manager_before.add('777', kinopoisk_id=435, title='Дюна', year=2021, poster_url=POSTER_URL) is True
    # Дубль — до «рестарта»: вторая запись не создаётся
    assert manager_before.add('777', kinopoisk_id=435, title='Дюна (копия)', year=2021, poster_url=None) is False

    # «Рестарт» процесса: новое приложение и новый менеджер на той же БД
    manager_after = WatchlistManager(app=pg_boot('kinobot_b9_wl_second'))
    assert manager_after.contains('777', 435) is True
    items, total = manager_after.list_page('777', 0, 5)
    assert total == 1
    assert items[0]['title'] == 'Дюна'
    assert items[0]['year'] == 2021
    assert items[0]['poster_url'] == POSTER_URL

    # Удаление новым экземпляром видно и старому (общая БД, не память)
    assert manager_after.remove('777', 435) is True
    assert manager_before.contains('777', 435) is False
    # Повторное удаление отсутствующей записи — штатные False, без исключений
    assert manager_after.remove('777', 435) is False


def test_feedback_upsert_survives_restart(pg_boot):
    """Реакция + оценка переживают «рестарт»; обновление оценки — одна строка.

    Upsert-семантика фидбека (критерий B9) проверяется СКВОЗЬ «рестарт»:
    новый экземпляр `FeedbackManager` видит прежнюю реакцию и оценку, а
    повторная оценка ОБНОВЛЯЕТ строку, а не плодит дубль пары
    (user_id, kinopoisk_id).
    """
    manager_before = FeedbackManager(app=pg_boot('kinobot_b9_fb_first'))
    assert manager_before.set_reaction('777', 435, 'watched') is True
    assert manager_before.set_rating('777', 435, 8) is True

    # «Рестарт»: новое приложение и новый менеджер фидбека на той же БД
    manager_after = FeedbackManager(app=pg_boot('kinobot_b9_fb_second'))
    feedback = manager_after.get_feedback('777', 435)
    assert feedback is not None
    assert feedback['reaction'] == 'watched'
    assert feedback['rating'] == 8

    # Обновление оценки после «рестарта» — upsert: ровно одна строка
    assert manager_after.set_rating('777', 435, 9) is True
    rows = manager_after.list_user_feedback('777')
    assert len(rows) == 1
    assert rows[0]['rating'] == 9
    assert rows[0]['reaction'] == 'watched'


# === 4. Ошибки: каждый класс → свой текст + кнопка (сводка B7) ===


_ERROR_TEXT_BY_CLASS = {
    telegram_bot.ERROR_CLASS_NETWORK: telegram_bot._ERROR_TEXT_NETWORK,
    telegram_bot.ERROR_CLASS_LLM: telegram_bot._ERROR_TEXT_LLM,
    telegram_bot.ERROR_CLASS_GENERIC: telegram_bot._ERROR_TEXT_GENERIC,
}


@pytest.mark.parametrize('exc,expected_class', [
    (aiohttp.ClientConnectionError('Кинопоиск не отвечает'), telegram_bot.ERROR_CLASS_NETWORK),
    (Exception('Ошибка получения токена GigaChat API'), telegram_bot.ERROR_CLASS_LLM),
    (ValueError('внезапный сбой'), telegram_bot.ERROR_CLASS_GENERIC),
])
def test_each_error_class_has_own_text_and_action_button(exc, expected_class):
    """Представитель класса → тот же класс в `build_error_reply`: свой текст, ≥1 кнопка."""
    assert telegram_bot.classify_error(exc) == expected_class

    text, markup = telegram_bot.build_error_reply(exc, 'last')

    assert text == _ERROR_TEXT_BY_CLASS[expected_class]
    buttons = all_buttons(markup)
    assert len(buttons) >= 1
    # Каждая кнопка — действие (callback), а не URL-заглушка без поведения
    assert all(b.callback_data for b in buttons)


def test_error_class_texts_are_pairwise_distinct():
    """Три класса ошибок — три РАЗНЫХ текста (приёмка B7/B9: не один генерик)."""
    texts = list(_ERROR_TEXT_BY_CLASS.values())
    assert len(set(texts)) == 3


# === 5. Диспетчеризация callback по префиксам фазы 1 (гэп 3) ===


# Контракт таблицы маршрутов: ВСЕ префиксы фазы 0+1 в фиксированном порядке
# (порядок важен: первое совпадение `startswith` выигрывает). Литералы, для
# которых нет публичных констант, — из исходника telegram_bot.py. Контракт
# пополняется префиксами последующих задач: T6 добавил маршрут меню жанров.
_PHASE1_ROUTE_PREFIXES: Tuple[str, ...] = (
    'info:', 'alt:', 'similar:', 'back:', 'random:', 'mood:', 'retry:',
    PAGE_CALLBACK_PREFIX, SAVE_CALLBACK_PREFIX, UNSAVE_CALLBACK_PREFIX,
    'watchlist:', WATCHLIST_PAGE_PREFIX, FEEDBACK_CALLBACK_PREFIX,
    telegram_bot._MENU_PREFIX, telegram_bot._TOP_PREFIX,
    telegram_bot._GENRE_PREFIX,
)

# Образец callback_data на каждый префикс (реальные формы из B1–B8)
_PREFIX_SAMPLES: Dict[str, str] = {
    'info:': 'info:447301',
    'alt:': 'alt:list',
    'similar:': 'similar:435',
    'back:': 'back:list',
    'random:': RANDOM_MOVIE_CALLBACK,
    'mood:': 'mood:start',
    'retry:': 'retry:last',
    PAGE_CALLBACK_PREFIX: f'{PAGE_CALLBACK_PREFIX}5:abcd1234',
    SAVE_CALLBACK_PREFIX: f'{SAVE_CALLBACK_PREFIX}435',
    UNSAVE_CALLBACK_PREFIX: f'{UNSAVE_CALLBACK_PREFIX}435',
    'watchlist:': 'watchlist:view',
    WATCHLIST_PAGE_PREFIX: f'{WATCHLIST_PAGE_PREFIX}5',
    FEEDBACK_CALLBACK_PREFIX: f'{FEEDBACK_CALLBACK_PREFIX}watched:435',
    telegram_bot._MENU_PREFIX: f'{telegram_bot._MENU_PREFIX}top',
    telegram_bot._TOP_PREFIX: f'{telegram_bot._TOP_PREFIX}50:movies',
    # T6: меню выбора жанра (образец — реальный callback кнопки жанра)
    telegram_bot._GENRE_PREFIX: f'{telegram_bot._GENRE_PICK_PREFIX}комедия',
}


def test_route_table_covers_all_phase1_prefixes_in_order():
    """Состав и порядок `_CALLBACK_ROUTES` — контракт диспетчеризации фазы 1."""
    prefixes = tuple(prefix for prefix, _ in telegram_bot._CALLBACK_ROUTES)
    assert prefixes == _PHASE1_ROUTE_PREFIXES


def test_dispatcher_routes_every_prefix_to_own_handler(monkeypatch):
    """Каждый префикс → ровно свой обработчик; на любой callback есть `answer()`.

    Обработчики подменены рекордерами (таблица `_CALLBACK_ROUTES` хранит
    прямые ссылки, поэтому monkeypatch'ится сама таблица): проверяется
    МАРШРУТИЗАЦИЯ, а не поведение маршрутов (оно — в тестах B1–B8).
    """
    original_routes = telegram_bot._CALLBACK_ROUTES
    calls: List[Tuple[str, str]] = []

    def _recorder(name: str):
        async def _handler(update, context, query, data):
            calls.append((name, data))

        return _handler

    monkeypatch.setattr(
        telegram_bot, '_CALLBACK_ROUTES',
        tuple((prefix, _recorder(prefix)) for prefix, _ in original_routes),
    )

    for prefix, _ in original_routes:
        data = _PREFIX_SAMPLES[prefix]
        calls.clear()
        update = FakeCallbackUpdate(data, from_user=FakeUser(777))
        run_coro(telegram_bot.handle_movie_detail(update, None))
        # Ровно одно срабатывание — своего префикса (коллизий startswith нет:
        # 'unsave:…' не уходит в 'save:', 'menu:…' — в 'mood:' и т.д.)
        assert calls == [(prefix, data)], f'префикс {prefix!r} маршрутизирован неверно'
        assert update.callback_query.answer_calls == 1

    # Неизвестный префикс: ни один маршрут не вызван, дружелюбный выход (B7)
    calls.clear()
    update = FakeCallbackUpdate('bogus:thing', from_user=FakeUser(777))
    run_coro(telegram_bot.handle_movie_detail(update, None))
    assert calls == []
    assert update.callback_query.answer_calls == 1
    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_UNKNOWN_CALLBACK
    assert all_buttons(kwargs['reply_markup'])


# === 6. Онбординг-кнопка random:movie (чек-лист B9) ===


def test_onboarding_random_movie_button_is_wired_to_route():
    """Клавиатура `/start`: `random:movie` первой кнопкой; маршруты привязаны.

    Сквозная связка B1+B8+B3: кнопка онбординга несёт callback `random:movie`,
    а таблица маршрутов отправляет префикс `random:` в обработчик случайного
    фильма (доставка карточки покрыта test_onboarding_b1 — здесь приёмка
    связки «кнопка → маршрут»). С B3 в клавиатуре третья кнопка «📌 Мой
    список» (`menu:watchlist`) — префикс `menu:` так же привязан к диспетчеру.
    """
    markup = telegram_bot.build_onboarding_keyboard()
    datas = [b.callback_data for b in all_buttons(markup)]
    assert datas == [
        RANDOM_MOVIE_CALLBACK,
        telegram_bot.MOOD_START_CALLBACK,
        f'{telegram_bot._MENU_PREFIX}watchlist',
    ]
    assert RANDOM_MOVIE_CALLBACK == 'random:movie'

    routes = dict(telegram_bot._CALLBACK_ROUTES)
    assert routes['random:'] is telegram_bot._handle_random_callback
    assert routes[telegram_bot._MENU_PREFIX] is telegram_bot._handle_menu_callback


# === 7. Единственный источник схем новых таблиц (database.py ↔ migrations/ ↔ init_db.py) ===


def test_new_tables_have_single_schema_source():
    """Схемы watchlist/movie_feedback определены РОВНО один раз — в моделях.

    Приёмка B9 «схемы новых таблиц идентичны» обеспечивается конструкцией:
    init_db.py — тонкая обёртка T14 (импортирует модели и накатывает схему
    миграциями через apply_database_migrations, единственный источник DDL —
    migrations/), определения НЕ дублируются — ни сырым CREATE TABLE/ALTER,
    ни повторными db.Column, ни вызовом db.create_all(). Состав колонок —
    контракт моделей фазы 1; согласованность моделей и миграций — guard T13.
    """
    text = (ROOT / 'init_db.py').read_text(encoding='utf-8')
    upper = text.upper()
    assert 'CREATE TABLE' not in upper
    assert 'ALTER' not in upper
    assert 'db.Column' not in text
    assert 'db.create_all()' not in text
    assert 'from models.database import' in text
    assert 'apply_database_migrations' in text

    tables = db_module.db.metadata.tables
    assert {'watchlist', 'movie_feedback'} <= set(tables)
    watchlist_cols = {c.name for c in Watchlist.__table__.columns}
    assert watchlist_cols == {'id', 'user_id', 'kinopoisk_id', 'title', 'year', 'poster_url', 'added_at'}
    feedback_cols = {c.name for c in MovieFeedback.__table__.columns}
    assert feedback_cols == {'id', 'user_id', 'kinopoisk_id', 'reaction', 'rating', 'created_at', 'updated_at'}


# === 8. Nit n5: десятилетие в справке — из config.CURRENT_YEAR ===


def test_help_decade_follows_current_year(monkeypatch):
    """Пример «лучшие фильмы …-х» в /help вычисляется из CURRENT_YEAR.

    При CURRENT_YEAR=2031 справка обещает «2030-х» (а не зашитое «2020-х»);
    четвёртый хардкод года не появляется — используется уже импортированная
    в telegram_bot константа config.CURRENT_YEAR (handoff B8, nit n5).
    """
    monkeypatch.setattr(telegram_bot, 'CURRENT_YEAR', 2031)
    message = FakeMessage(chat=FakeChat())
    update = SimpleNamespace(message=message, effective_user=FakeUser(777))

    run_coro(telegram_bot.handle_help(update, None))

    text = message.texts[0][0]
    assert '2030-х' in text
    assert '2020-х' not in text


def test_help_has_no_hardcoded_decade_in_source():
    """В исходнике telegram_bot.py нет вручную зашитого «2020-х»."""
    source = (ROOT / 'src' / 'telegram_bot.py').read_text(encoding='utf-8')
    assert '2020-х' not in source
    assert 'CURRENT_YEAR // 10 * 10' in source
