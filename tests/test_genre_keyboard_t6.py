"""Тесты T6: inline-меню выбора жанра (`genre:pick:{имя}`).

Изменение `add-mood-genre-inline-keyboards-t5t6` (бэклог
`backlog/backlog_2026-10-04_bugfix_telegram_ui.md`, задача T6, баг 3b;
риск 5 — формулировка запроса обязана классифицироваться существующими
промптами, риск 9 — лимит `callback_data` 64 БАЙТА).

Покрытие (design.md D10, наследует D1-D9):
- структура меню: 12 кнопок жанров по 2 в ряд + ряд «⬅️ Назад»
  (`genre:menu`), состав и ПОРЯДОК == `GENRE_MENU_NAMES`, подписи — имя
  жанра с заглавной буквы, лимиты Bot API (≤64 символа подпись, ≤64 байта
  `callback_data`, сумма подписей ряда ≤44 — эвристика T7 заложена заранее);
- инвариант «единый словарь»: каждое имя кнопки входит в множество значений
  `dialogue_manager.mood_to_genre` (защита от рассинхрона — второй
  независимый список жанров запрещён) и НЕ входит в
  `movie_filter.EXCLUDED_GENRES` (иначе кнопка давала бы пустую выдачу);
- словарь запросов ВЫВОДИТСЯ из `GENRE_QUERY_TEMPLATE` (единый источник
  формулировки, образец `_TOP_QUERY_BY_SEGMENT`), год не используется;
- приглашение `_GENRE_PROMPT_TEXT` с клавиатурой в точках входа `/genre` и
  `menu:genre` — текст НЕ изменился (контракт tests/test_menu_callbacks_b8.py);
- dispatch `genre:pick:{имя}` (по всем 12): в `_run_dialogue_query` ушёл
  текст из словаря, клик учтён, `answer()` выполнен, ошибки нет;
- `genre:menu` — возврат к главному меню;
- неизвестный сегмент и пустой сегмент — ответ B7 с кнопками выхода (без
  dead-end, без исключения);
- сбой пайплайна — классифицированная ошибка с целью повтора
  `retry:genre:pick:{имя}` и повтор ТОЙ ЖЕ операции через диспетчер;
- отсутствие коллизий префиксов: `genre:` уникален, `menu:genre` остаётся в
  маршруте меню, `retry:genre:…` — в маршруте повтора;
- риск 5: детерминированная проверка `IntentClassifier._classify_fallback`
  для всех 12 запросов (intent='initial', жанр извлечён) БЕЗ сети и БЕЗ
  правки промптов; ожидания зафиксированы по фактическому поведению кода.

Все проверки офлайн: фейки PTB/aiohttp из `tests/conftest.py`, LLM-пайплайн
мокируется (`dm.process_message = AsyncMock(...)`), реальные Telegram,
PostgreSQL и LLM не используются.
"""
import logging
import math
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest
from telegram.error import NetworkError

import telegram_bot
from conftest import (
    FakeCallbackUpdate,
    FakeMessage,
    FakeUser,
    all_buttons,
    install_callback_mocks,
    make_manager,
    run_coro,
)
from dialogue_manager import GENRE_MENU_NAMES, GENRE_QUERIES, GENRE_QUERY_TEMPLATE, RANDOM_MOVIE_CALLBACK
from utils.movie_filter import EXCLUDED_GENRES

ROOT = Path(__file__).resolve().parents[1]
GUIDELINE_PATH = ROOT / 'docs' / 'emoji_guideline.md'

TG_USER_ID = '777'
CALLBACK_DATA_LIMIT_BYTES = 64  # лимит Bot API для `callback_data` (БАЙТЫ)
ROW_LABEL_SUM_LIMIT = 44  # эвристика читаемости ряда (закрепит guard-тест T7)

_GENRE_NAMES: List[str] = list(GENRE_MENU_NAMES)
_PICK = telegram_bot._GENRE_PICK_PREFIX
_MENU_SEGMENT = telegram_bot._GENRE_MENU_SEGMENT

# Фактические жанры детерминированного fallback-классификатора
# (`IntentClassifier._classify_fallback`, словарь `genre_mapping`) для
# запросов `GENRE_QUERIES`. Ожидания сняты ПО КОДУ, а не «на глаз»:
# 11 жанров извлекаются точно, а «мелодрама» на деградированном пути даёт
# более грубый «драма» — в `genre_mapping` основа 'драм' стоит РАНЬШЕ
# 'мелодрам' и цикл прерывается на первом совпадении. LLM-ветка извлекает
# жанр точно; fallback — резерв при сбое LLM, поэтому грубость допустима и
# зафиксирована здесь явно (риск 5 бэклога).
_FALLBACK_GENRE_BY_NAME: Dict[str, str] = {name: name for name in _GENRE_NAMES}
_FALLBACK_GENRE_BY_NAME['мелодрама'] = 'драма'

# Тексты ошибок бота: ни один из них не должен показываться в штатном пути
_ERROR_TEXTS = (
    telegram_bot._ERROR_TEXT_NETWORK,
    telegram_bot._ERROR_TEXT_LLM,
    telegram_bot._ERROR_TEXT_GENERIC,
    telegram_bot._ERROR_TEXT_UNKNOWN_CALLBACK,
    telegram_bot._ERROR_TEXT_LOST_RETRY,
)


# nit 4 ревью: значение по умолчанию не должно создаваться в сигнатуре
# (вызов в аргументе вычисляется один раз). Явный None при этом означает
# «инициатор не определён» — его используют тесты guard'а, поэтому маркер
# по умолчанию отделён от None.
_DEFAULT_USER = object()


def _dispatch(data: str, message: Any = None, from_user: Any = _DEFAULT_USER, context: Any = None) -> FakeCallbackUpdate:
    """Прогнать callback через ОБЩИЙ диспетчер (ловит и коллизии префиксов)."""
    user = FakeUser(777) if from_user is _DEFAULT_USER else from_user
    update = FakeCallbackUpdate(data, message=message, from_user=user)
    run_coro(telegram_bot.handle_movie_detail(update, context))
    return update


class _GenreCommandUpdate:
    """Update команды /genre: сообщение + инициатор (паттерн T5/B1)."""

    def __init__(self, user_id: int = 777):
        self.message = FakeMessage()
        self.effective_user = FakeUser(user_id)


def _callbacks(markup: Any) -> List[str]:
    return [b.callback_data for b in all_buttons(markup)]


def _genre_buttons(markup: Any) -> List[Any]:
    """Кнопки жанров меню (без ряда «⬅️ Назад»)."""
    return [b for b in all_buttons(markup) if b.callback_data.startswith(_PICK)]


def _expected_callback_data() -> List[str]:
    return [f'{_PICK}{name}' for name in _GENRE_NAMES]


def _sent_texts(update: Any) -> List[str]:
    return [text for text, _ in update.callback_query.message.texts]


def _stub_query_runner(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Подменить `_run_dialogue_query` рекордером (без LLM, сети и БД)."""
    runner = AsyncMock()
    monkeypatch.setattr(telegram_bot, '_run_dialogue_query', runner)
    return runner


def _stub_pipeline(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Менеджер диалога с мокированным LLM-пайплайном и фейками conftest."""
    dm = make_manager()
    dm.process_message = AsyncMock(return_value={'response': 'Вот подборка', 'reply_markup': None})  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)
    return dm


# === 1. Структура меню жанров (design.md D10, образец D2) ===


def test_genre_menu_rows_and_order_follow_names():
    """6 рядов по 2 кнопки в порядке `GENRE_MENU_NAMES` + ряд «⬅️ Назад»."""
    rows = telegram_bot.build_genre_menu().inline_keyboard

    expected_rows = math.ceil(len(_GENRE_NAMES) / telegram_bot._GENRE_BUTTONS_PER_ROW)
    assert len(rows) == expected_rows + 1, 'число рядов не соответствует раскладке по 2 кнопки'
    genre_rows, back_row = rows[:-1], rows[-1]
    for row in genre_rows:
        assert len(row) <= telegram_bot._GENRE_BUTTONS_PER_ROW
    # nit 5 ревью: `ceil`, а не `//` — формула остаётся верной и при нечётном
    # числе жанров (последний ряд неполный), как в tests/test_mood_keyboard_t5.py
    assert all(len(row) == telegram_bot._GENRE_BUTTONS_PER_ROW for row in genre_rows[:-1])
    assert [b.callback_data for row in genre_rows for b in row] == _expected_callback_data()
    # «⬅️ Назад» — сегмент того же маршрута (паритет с `top:menu` подменю топов)
    assert len(back_row) == 1
    assert back_row[0].callback_data == _MENU_SEGMENT
    assert back_row[0].text == '⬅️ Назад'


def test_genre_labels_are_capitalized_names_without_emoji():
    """Подпись — имя жанра с заглавной буквы, БЕЗ эмодзи (чек-лист п.1 B6)."""
    labels = {b.callback_data: b.text for b in _genre_buttons(telegram_bot.build_genre_menu())}

    assert len(labels) == len(_GENRE_NAMES)
    for name in _GENRE_NAMES:
        label = labels[f'{_PICK}{name}']
        assert label == name.capitalize(), f'подпись жанра «{name}» не выведена из имени'
        assert label[0].isalpha(), f'подпись «{label}» начинается не с буквы (эмодзи не нужны)'
        assert label[0].isupper()


def test_genre_buttons_do_not_introduce_new_emoji():
    """Новые эмодзи в гайдлайн не добавлены: 🎬 остаётся за «фильм»/меню жанра."""
    guideline = GUIDELINE_PATH.read_text(encoding='utf-8')
    labels = [b.text for b in all_buttons(telegram_bot.build_genre_menu())]

    # Единственный эмодзи меню жанров — «⬅️ Назад», уже закреплённый гайдлайном
    emoji_labels = [label for label in labels if not label[0].isalpha()]
    assert emoji_labels == ['⬅️ Назад']
    assert any('⬅️' in line for line in guideline.splitlines())
    # 🎬 по-прежнему закреплён за смыслом «фильм» и используется в пункте меню
    assert '🎬 Поиск по жанру' in [b.text for b in all_buttons(telegram_bot.get_main_menu())]


def test_genre_menu_fits_telegram_limits():
    """Подписи ≤64 символа, `callback_data` ≤64 БАЙТ, сумма подписей ряда ≤44."""
    for row in telegram_bot.build_genre_menu().inline_keyboard:
        assert sum(len(b.text) for b in row) <= ROW_LABEL_SUM_LIMIT, 'ряд кнопок слишком широкий'
        for button in row:
            assert len(button.text) <= 64
            size = len(button.callback_data.encode('utf-8'))
            assert size <= CALLBACK_DATA_LIMIT_BYTES, f'callback_data {size} байт: {button.callback_data!r}'


def test_retry_target_of_genre_pick_fits_byte_limit():
    """Цель повтора `retry:genre:pick:{имя}` тоже укладывается в 64 байта (риск 9)."""
    for name in _GENRE_NAMES:
        target = f'retry:{_PICK}{name}'
        assert len(target.encode('utf-8')) <= CALLBACK_DATA_LIMIT_BYTES, target


# === 2. Инварианты «единого словаря» (design.md D10, риск T5/T6) ===


def test_genre_names_are_subset_of_mood_to_genre_values():
    """Каждое имя кнопки — жанр из `mood_to_genre` (второй список запрещён)."""
    dm = make_manager()
    known = {genre for genres in dm.mood_to_genre.values() for genre in genres}

    missing = set(_GENRE_NAMES) - known
    assert not missing, f'имена кнопок не входят в значения mood_to_genre: {sorted(missing)}'
    # Поднабор осознанный (бэклог: ~10-14 кнопок), поэтому фиксируем размер
    assert len(_GENRE_NAMES) == 12
    assert len(set(_GENRE_NAMES)) == len(_GENRE_NAMES), 'в меню жанров есть дубли'


def test_genre_names_are_not_excluded_genres():
    """Ни одно имя кнопки не входит в `EXCLUDED_GENRES` (иначе пустая выдача)."""
    assert not set(_GENRE_NAMES) & set(EXCLUDED_GENRES)


def test_genre_queries_are_derived_from_single_template():
    """Словарь запросов выводится из шаблона; ключи == `GENRE_MENU_NAMES`."""
    queries = GENRE_QUERIES

    assert list(queries) == _GENRE_NAMES
    for name, text in queries.items():
        assert text == GENRE_QUERY_TEMPLATE.format(genre=name)
        assert name in text
        # Год не используется: четвёртый хардкод CURRENT_YEAR запрещён (риск 6)
        assert str(telegram_bot.CURRENT_YEAR) not in text


# === 3. Приглашение с клавиатурой (design.md D10, текст НЕ меняется) ===


@pytest.mark.parametrize('entry_point', ['/genre', 'menu:genre'])
def test_genre_prompt_is_delivered_with_keyboard(monkeypatch, entry_point: str):
    """Тот же текст `_GENRE_PROMPT_TEXT` + inline-меню выбора жанра."""
    dm = _stub_pipeline(monkeypatch)

    if entry_point == '/genre':
        command_update = _GenreCommandUpdate()
        run_coro(telegram_bot.handle_genre_command(command_update, None))
        text, kwargs = command_update.message.texts[0]
    else:
        callback_update = _dispatch(entry_point)
        text, kwargs = callback_update.callback_query.message.texts[0]

    assert text == telegram_bot._GENRE_PROMPT_TEXT, 'текст приглашения изменён (контракт B8)'
    assert [b.callback_data for b in _genre_buttons(kwargs['reply_markup'])] == _expected_callback_data()
    assert _MENU_SEGMENT in _callbacks(kwargs['reply_markup'])
    dm.process_message.assert_not_awaited()


# === 4. Тап кнопки жанра (design.md D10, образец `_handle_top_callback`) ===


@pytest.mark.parametrize('genre_name', _GENRE_NAMES)
def test_genre_pick_runs_pipeline_with_dictionary_query(monkeypatch, genre_name: str):
    """Каждая из 12 кнопок запускает пайплайн с текстом из словаря запросов."""
    runner = _stub_query_runner(monkeypatch)
    tracked: List[str] = []
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda key: tracked.append(key))

    update = _dispatch(f'{_PICK}{genre_name}')

    runner.assert_awaited_once()
    args = runner.await_args.args
    assert args[2] == TG_USER_ID
    assert args[3] == GENRE_QUERIES[genre_name], 'в пайплайн ушёл не текст словаря'
    assert tracked == [f'tg:{TG_USER_ID}'], 'клик по кнопке жанра не учтён в статистике'
    assert update.callback_query.answer_calls == 1
    assert not [t for t in _sent_texts(update) if t in _ERROR_TEXTS], 'показано сообщение об ошибке'


def test_genre_pick_end_to_end_passes_query_verbatim(monkeypatch):
    """Сквозной сценарий (LLM-ветка): запрос доходит до `process_message` дословно."""
    dm = _stub_pipeline(monkeypatch)

    update = _dispatch(f'{_PICK}комедия')

    dm.process_message.assert_awaited_once()
    args = dm.process_message.await_args.args
    assert args[1] == TG_USER_ID
    assert args[2] == GENRE_QUERIES['комедия'] == 'посоветуй лучшие фильмы в жанре комедия'
    assert 'Вот подборка' in _sent_texts(update)


def test_genre_pick_without_user_is_friendly(monkeypatch):
    """Инициатор не определён — подсказка написать запрос текстом (fallback)."""
    runner = _stub_query_runner(monkeypatch)

    update = _dispatch(f'{_PICK}комедия', from_user=None)

    text, kwargs = update.callback_query.message.texts[0]
    assert 'Не удалось определить пользователя' in text
    assert GENRE_QUERIES['комедия'] in text, 'нет подсказки о рабочем fallback'
    assert kwargs.get('reply_markup') is not None, 'служебный ответ без действия-выхода (B7)'
    runner.assert_not_awaited()


# === 5. Сегмент `genre:menu` — возврат к главному меню ===


def test_genre_menu_segment_returns_main_menu(monkeypatch):
    """«⬅️ Назад» меню жанров рисует главное меню (чистый рендер, без БД/LLM)."""
    dm = _stub_pipeline(monkeypatch)

    update = _dispatch(_MENU_SEGMENT)

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._BACK_TO_MAIN_TEXT
    assert _callbacks(kwargs['reply_markup']) == _callbacks(telegram_bot.get_main_menu())
    dm.process_message.assert_not_awaited()
    assert not [t for t in _sent_texts(update) if t in _ERROR_TEXTS]


# === 6. Неизвестный сегмент — ответ B7 (без dead-end) ===


@pytest.mark.parametrize('data', [f'{_PICK}опера', f'{_PICK}комедия ', 'genre:', 'genre:unknown'])
def test_unknown_genre_segment_is_friendly(monkeypatch, caplog, data: str):
    """Неизвестный/пустой сегмент — объяснение с кнопками выхода, не молчание."""
    runner = _stub_query_runner(monkeypatch)

    with caplog.at_level(logging.WARNING):
        update = _dispatch(data)

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_UNKNOWN_CALLBACK
    assert _callbacks(kwargs['reply_markup']) == ['back:list', RANDOM_MOVIE_CALLBACK]
    assert update.callback_query.answer_calls == 1
    assert data in caplog.text
    runner.assert_not_awaited()


# === 7. Сбой пайплайна и повтор ТОЙ ЖЕ операции (design.md D10) ===


def test_genre_pick_failure_offers_retry_of_same_genre(monkeypatch):
    """Сбой подбора — классифицированная ошибка с целью `retry:genre:pick:{имя}`."""
    dm = make_manager()
    dm.process_message = AsyncMock(side_effect=NetworkError('нет связи'))  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)

    update = _dispatch(f'{_PICK}комедия')

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_NETWORK
    assert f'retry:{_PICK}комедия' in _callbacks(kwargs['reply_markup'])


def test_retry_genre_pick_repeats_same_query(monkeypatch):
    """`retry:genre:pick:комедия` через диспетчер снова запускает тот же подбор."""
    runner = _stub_query_runner(monkeypatch)

    update = _dispatch(f'retry:{_PICK}комедия')

    runner.assert_awaited_once()
    assert runner.await_args.args[3] == GENRE_QUERIES['комедия']
    assert not [t for t in _sent_texts(update) if t in _ERROR_TEXTS], (
        'повтор ушёл в «неизвестная цель повтора» вместо подбора'
    )


def test_retry_genre_menu_returns_main_menu(monkeypatch):
    """Повтор сегмента `genre:menu` — тот же возврат к главному меню."""
    _stub_pipeline(monkeypatch)

    update = _dispatch(f'retry:{_MENU_SEGMENT}')

    assert update.callback_query.message.texts[0][0] == telegram_bot._BACK_TO_MAIN_TEXT


# === 8. Отсутствие коллизий префиксов (design.md D10) ===


def test_route_table_has_unique_genre_prefix():
    """Префикс `genre:` в таблице ровно один, дублей префиксов нет."""
    prefixes = [prefix for prefix, _ in telegram_bot._CALLBACK_ROUTES]

    assert prefixes.count(telegram_bot._GENRE_PREFIX) == 1
    assert len(prefixes) == len(set(prefixes))
    assert prefixes[-1] == telegram_bot._GENRE_PREFIX, 'новый маршрут добавлен не в конец таблицы'


@pytest.mark.parametrize('data,expected', [
    (f'{_PICK}комедия', ['genre:']),
    (_MENU_SEGMENT, ['genre:']),
    ('menu:genre', ['menu:']),
    (f'retry:{_PICK}комедия', ['retry:']),
    ('genre', []),
    ('genre:', ['genre:']),
])
def test_genre_data_matches_only_expected_route(data: str, expected: List[str]):
    """`startswith`-коллизий нет: каждый callback попадает в свой маршрут."""
    matched = [prefix for prefix, _ in telegram_bot._CALLBACK_ROUTES if data.startswith(prefix)]

    assert matched == expected


def test_menu_genre_still_routed_to_menu_handler(monkeypatch):
    """Регресс: `menu:genre` обрабатывает маршрут меню, а не маршрут жанра."""
    _stub_pipeline(monkeypatch)
    calls: List[str] = []

    async def _recorder(update, context, query, data):
        calls.append(data)

    routes = tuple(
        (prefix, _recorder) if prefix == telegram_bot._GENRE_PREFIX else (prefix, handler)
        for prefix, handler in telegram_bot._CALLBACK_ROUTES
    )
    monkeypatch.setattr(telegram_bot, '_CALLBACK_ROUTES', routes)

    update = _dispatch('menu:genre')

    assert calls == [], 'маршрут жанра перехватил чужой callback `menu:genre`'
    assert update.callback_query.message.texts[0][0] == telegram_bot._GENRE_PROMPT_TEXT


# === 9. Риск 5: классификация формулировки БЕЗ сети и БЕЗ правки промптов ===


@pytest.mark.parametrize('genre_name', _GENRE_NAMES)
def test_genre_query_is_classified_as_initial_with_genre(genre_name: str):
    """Fallback-классификатор: intent='initial' и непустой жанр для всех 12.

    Реальный LLM не вызывается (офлайн-контракт тестов), промпты
    `src/prompts/*.txt` не меняются: проверяется детерминированный резервный
    путь `IntentClassifier._classify_fallback` — он же гарантирует, что
    формулировка «посоветуй лучшие фильмы в жанре X» не отсечётся
    offtopic-предпроверкой и не уйдёт в ветку `info`.
    """
    classifier = make_manager().intent_classifier

    params = classifier._classify_fallback(GENRE_QUERIES[genre_name])

    assert params['intent'] == 'initial'
    assert params['genre'] == _FALLBACK_GENRE_BY_NAME[genre_name]
    assert params['movie_type'] == 'movie'
    # «лучшие» повышает минимальный рейтинг — ожидаемо и полезно для подбора
    assert params['min_rating'] == 7.0
    # Настроение не загрязняется: запрос жанра не должен уходить в mood-маршрут
    assert params['mood'] is None


def test_fallback_genre_expectations_match_actual_code():
    """Ожидания fallback сняты по коду: словарь совпадает с фактическим выводом."""
    classifier = make_manager().intent_classifier
    actual = {
        name: classifier._classify_fallback(GENRE_QUERIES[name])['genre']
        for name in _GENRE_NAMES
    }

    assert actual == _FALLBACK_GENRE_BY_NAME
    # Единственное огрубление резервного пути — «мелодрама» → «драма»
    assert [n for n in _GENRE_NAMES if _FALLBACK_GENRE_BY_NAME[n] != n] == ['мелодрама']
