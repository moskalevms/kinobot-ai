"""Тесты T5: inline-клавиатура выбора настроения (`mood:pick:{ключ}`).

Изменение `add-mood-genre-inline-keyboards-t5t6` (бэклог
`backlog/backlog_2026-10-04_bugfix_telegram_ui.md`, задача T5, баг 3a).

Покрытие (design.md D1-D9):
- структура клавиатуры: 8 кнопок настроений по 2 в ряд + заключительный ряд
  выхода в хаб «🏠 Меню» (`menu:main`; до C2 — «⬅️ Назад», правило двух
  стилей docs/emoji_guideline.md), состав и ПОРЯДОК — обход `mood_triggers` (единственный
  источник, второй хардкод-список запрещён), лимиты Bot API
  (подпись ≤64 символа, `callback_data` ≤64 БАЙТ в UTF-8);
- приглашение `_MOOD_PROMPT_HTML` с клавиатурой во всех точках входа
  (`/mood`, `mood:start`, `menu:mood`, `retry:mood`) — текст и parse_mode
  не изменились;
- dispatch `mood:pick:{ключ}` (по всем 8 ключам): в пайплайн ушла ПЕРВАЯ
  фраза-триггер словаря, клик учтён, `answer()` выполнен, ошибки нет;
- `_mark_awaiting_mood` НЕ вызывается на кнопочном пути (детерминированный
  prefilter B6 не зависит от in-memory признака) и вызывается на пути
  приглашения;
- неизвестный ключ / пустой список фраз — штатный ответ B7 с кнопками выхода
  (без dead-end, без исключения);
- сбой пайплайна — классифицированная ошибка с целью повтора
  `retry:mood:pick:{ключ}` и повтор ТОЙ ЖЕ операции через диспетчер;
- инварианты словарей: ключи подписей == ключи `mood_triggers`, первая фраза
  каждого ключа распознаётся `_match_mood_prefilter` этим же ключом;
- отсутствие коллизий префиксов: `mood:start` по-прежнему показывает
  приглашение, новый префикс в `_CALLBACK_ROUTES` не появился;
- резервная подпись и warning для ключа без подписи (бот не падает).

Все проверки офлайн: фейки PTB/aiohttp из `tests/conftest.py`, LLM-пайплайн
мокируется (`dm.process_message = AsyncMock(...)`), реальные Telegram,
PostgreSQL и LLM не используются.
"""
import logging
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
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
    make_list_movies,
    make_manager,
    run_coro,
)
from dialogue_manager import AWAITING_MOOD_KEY, BUTTON_TEXT_LIMIT, MOOD_BUTTON_LABELS, RANDOM_MOVIE_CALLBACK

ROOT = Path(__file__).resolve().parents[1]
GUIDELINE_PATH = ROOT / 'docs' / 'emoji_guideline.md'

TG_USER_ID = '777'
# Лимит Bot API для `callback_data` — БАЙТЫ, не символы (кириллица стоит 2
# байта в UTF-8), поэтому проверяется именно `len(data.encode('utf-8'))`
CALLBACK_DATA_LIMIT_BYTES = 64

# Ключи и фразы-триггеры берутся ИЗ словаря настроений (design.md D1):
# захардкоженный список в тесте молча рассинхронизировался бы с
# `mood_triggers`, а параметризация потеряла бы смысл. Словарь — уровень
# экземпляра `DialogueManager.__init__`, поэтому источник — `make_manager()`.
_MOOD_TRIGGERS = make_manager().mood_triggers
_MOOD_KEYS: List[str] = list(_MOOD_TRIGGERS)
_PICK = telegram_bot._MOOD_PICK_PREFIX

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


class _MoodCommandUpdate:
    """Update команды /mood: сообщение + инициатор (паттерн test_mood_offtopic_b1)."""

    def __init__(self, user_id: int = 777):
        self.message = FakeMessage()
        self.effective_user = FakeUser(user_id)


def _callbacks(markup: Any) -> List[str]:
    return [b.callback_data for b in all_buttons(markup)]


def _mood_buttons(markup: Any) -> List[Any]:
    """Кнопки настроений клавиатуры (без ряда выхода в хаб «🏠 Меню»)."""
    return [b for b in all_buttons(markup) if b.callback_data.startswith(_PICK)]


def _expected_callback_data() -> List[str]:
    return [f'{_PICK}{key}' for key in _MOOD_KEYS]


def _sent_texts(update: Any) -> List[str]:
    return [text for text, _ in update.callback_query.message.texts]


def _stub_pipeline(monkeypatch: pytest.MonkeyPatch, dm: Any = None) -> Any:
    """Менеджер диалога с мокированным LLM-пайплайном и фейками conftest."""
    manager = dm if dm is not None else make_manager()
    manager.process_message = AsyncMock(return_value={'response': 'Вот подборка', 'reply_markup': None})  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, manager)
    return manager


# === 1. Структура клавиатуры (design.md D1/D2/D8) ===


def test_mood_keyboard_rows_and_order_follow_dictionary():
    """4 ряда по 2 кнопки в порядке `mood_triggers` + отдельный ряд «🏠 Меню» (C2)."""
    keyboard = telegram_bot.build_mood_keyboard()
    rows = keyboard.inline_keyboard

    expected_rows = math.ceil(len(_MOOD_KEYS) / telegram_bot._MOOD_BUTTONS_PER_ROW)
    assert len(rows) == expected_rows + 1, 'число рядов не соответствует раскладке по 2 кнопки'
    mood_rows, back_row = rows[:-1], rows[-1]
    # Все ряды кнопок настроений — ровно по 2 (последний может быть неполным
    # при нечётном числе ключей словаря)
    for row in mood_rows:
        assert len(row) <= telegram_bot._MOOD_BUTTONS_PER_ROW
    assert all(len(row) == telegram_bot._MOOD_BUTTONS_PER_ROW for row in mood_rows[:-1])
    # Состав и ПОРЯДОК — обход словаря настроений
    assert [b.callback_data for row in mood_rows for b in row] == _expected_callback_data()
    # Заключительный ряд возвращает главное меню (переиспользование маршрута
    # menu:). C2 (unify-back-button-emoji-c2): подпись — «🏠 Меню», потому что
    # правило двух стилей гайдлайна привязывает 🏠 к ЛЮБОЙ кнопке с
    # `callback_data='menu:main'`; ⬅️ остаётся за контекстным возвратом.
    # `callback_data` при этом НЕ изменился (прежний маршрут хаба).
    assert len(back_row) == 1
    assert back_row[0].callback_data == f'{telegram_bot._MENU_PREFIX}main'
    assert back_row[0].callback_data == telegram_bot.MENU_MAIN_CALLBACK == 'menu:main'
    assert back_row[0].text == telegram_bot.truncate_button_text(telegram_bot.MENU_BUTTON_TEXT)
    assert back_row[0].text.startswith('🏠')


def test_mood_keyboard_fits_telegram_limits():
    """Подписи ≤64 символа, `callback_data` ≤64 БАЙТ в UTF-8 (риск 9 бэклога)."""
    for button in all_buttons(telegram_bot.build_mood_keyboard()):
        assert len(button.text) <= BUTTON_TEXT_LIMIT, f'подпись длиннее лимита: {button.text!r}'
        size = len(button.callback_data.encode('utf-8'))
        assert size <= CALLBACK_DATA_LIMIT_BYTES, f'callback_data {size} байт: {button.callback_data!r}'


def test_mood_button_labels_are_readable_and_guideline_based():
    """Подписи человекочитаемы, а их эмодзи закреплены гайдлайном за кнопкой."""
    guideline = GUIDELINE_PATH.read_text(encoding='utf-8')
    labels = {b.callback_data: b.text for b in _mood_buttons(telegram_bot.build_mood_keyboard())}

    assert len(labels) == len(_MOOD_KEYS)
    for key in _MOOD_KEYS:
        label = labels[f'{_PICK}{key}']
        assert label != key, f'подпись ключа «{key}» не человекочитаема'
        # T10: словарь подписей — единый источник в dialogue_manager.py
        assert label == MOOD_BUTTON_LABELS[key]
        # Эмодзи — в начале подписи (правило 2 гайдлайна) и ровно один
        emoji = label[0]
        assert not emoji.isalpha(), f'подпись «{label}» не начинается с эмодзи'
        lines = [line for line in guideline.splitlines() if emoji in line]
        # minor 3 ревью: строка гайдлайна обязана закреплять символ ИМЕННО за
        # кнопкой проверяемого ключа (`mood:pick:{key}`), а не «за какой-нибудь
        # кнопкой настроения»: иначе один символ можно было бы переиспользовать
        # в чужом смысле, нарушив принцип «1 эмодзи = 1 смысл»
        assert any(f'mood:pick:{key}' in line for line in lines), (
            f'эмодзи подписи «{label}» (U+{ord(emoji):04X}) не закреплён гайдлайном '
            f'за кнопкой `mood:pick:{key}`'
        )
        assert sum(1 for ch in label if not ch.isalpha() and not ch.isspace()) == 1, (
            f'в подписи «{label}» больше одного эмодзи/служебного символа'
        )


def test_unknown_dictionary_key_falls_back_with_warning(monkeypatch, caplog):
    """Ключ без подписи не роняет бота: резерв `key.title()` + warning (D1)."""
    stub = SimpleNamespace(mood_triggers={'устал': ['устал'], 'новыйключ': ['новый']})
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', stub)

    with caplog.at_level(logging.WARNING):
        keyboard = telegram_bot.build_mood_keyboard()

    labels = {b.callback_data: b.text for b in all_buttons(keyboard)}
    assert labels[f'{_PICK}устал'] == MOOD_BUTTON_LABELS['устал']
    assert labels[f'{_PICK}новыйключ'] == 'Новыйключ'
    assert 'новыйключ' in caplog.text, 'рассинхрон словарей не попал в журнал'


# === 2. Приглашение с клавиатурой во всех точках входа (design.md D5) ===


@pytest.mark.parametrize('entry_point', ['/mood', 'mood:start', 'menu:mood', 'retry:mood'])
def test_mood_prompt_is_delivered_with_keyboard(monkeypatch, entry_point: str):
    """Текст приглашения и parse_mode прежние, клавиатура выбора приложена."""
    dm = _stub_pipeline(monkeypatch)

    if entry_point == '/mood':
        command_update = _MoodCommandUpdate()
        run_coro(telegram_bot.handle_mood_command(command_update, None))
        text, kwargs = command_update.message.texts[0]
    else:
        callback_update = _dispatch(entry_point)
        text, kwargs = callback_update.callback_query.message.texts[0]

    assert text == telegram_bot._MOOD_PROMPT_HTML, 'текст приглашения изменён (контракт B1/B4/B8)'
    assert kwargs.get('parse_mode') == 'HTML'
    assert [b.callback_data for b in _mood_buttons(kwargs['reply_markup'])] == _expected_callback_data()
    # LLM-пайплайн на показе приглашения не запускается
    dm.process_message.assert_not_awaited()


def test_mood_command_marks_awaiting_flag(monkeypatch):
    """Регресс B1: команда /mood по-прежнему ставит признак ожидания ответа."""
    _stub_pipeline(monkeypatch)
    marker = AsyncMock()
    monkeypatch.setattr(telegram_bot, '_mark_awaiting_mood', marker)
    update = _MoodCommandUpdate()

    run_coro(telegram_bot.handle_mood_command(update, None))

    marker.assert_awaited_once_with(TG_USER_ID)
    assert update.message.texts[0][1]['reply_markup'] is not None


# === 3. Тап кнопки настроения: детерминированный подбор (design.md D4) ===


@pytest.mark.parametrize('mood_key', _MOOD_KEYS)
def test_mood_pick_runs_pipeline_with_first_trigger_phrase(monkeypatch, mood_key: str):
    """Каждая из 8 кнопок запускает пайплайн с ПЕРВОЙ фразой-триггером ключа."""
    dm = _stub_pipeline(monkeypatch)
    tracked: List[str] = []
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda key: tracked.append(key))

    update = _dispatch(f'{_PICK}{mood_key}')

    dm.process_message.assert_awaited_once()
    args = dm.process_message.await_args.args
    assert args[1] == TG_USER_ID
    assert args[2] == _MOOD_TRIGGERS[mood_key][0], 'в пайплайн ушла не первая фраза-триггер словаря'
    assert tracked == [f'tg:{TG_USER_ID}'], 'клик по кнопке не учтён в клиентской статистике'
    assert update.callback_query.answer_calls == 1
    assert not [t for t in _sent_texts(update) if t in _ERROR_TEXTS], 'показано сообщение об ошибке'


def test_mood_pick_end_to_end_without_llm_classifier(monkeypatch):
    """Сквозной сценарий: тап «😌 Устал» → подборка БЕЗ вызова LLM-классификатора."""
    dm = make_manager()
    install_callback_mocks(monkeypatch, dm)
    dm.movie_agent.recommend_movies = AsyncMock(return_value=make_list_movies(3))
    # Ответ LLM, который породил баг B1: если детерминированный маршрут
    # сработал, классификатор не вызывался вовсе
    dm.intent_classifier.classify_with_llm = AsyncMock(return_value={'intent': 'offtopic'})

    update = _dispatch(f'{_PICK}устал')

    dm.intent_classifier.classify_with_llm.assert_not_awaited()
    assert any('Фильм 1' in text for text in _sent_texts(update)), 'подборка не доставлена пользователю'
    assert not [t for t in _sent_texts(update) if t in _ERROR_TEXTS]


def test_mood_pick_without_user_is_friendly(monkeypatch):
    """Инициатор не определён — подсказка написать настроение текстом, без падения."""
    dm = _stub_pipeline(monkeypatch)

    update = _dispatch(f'{_PICK}устал', from_user=None)

    text, kwargs = update.callback_query.message.texts[0]
    assert 'Не удалось определить пользователя' in text
    assert 'настроение текстом' in text, 'нет подсказки о рабочем fallback (ручной ввод)'
    assert kwargs.get('parse_mode') == 'HTML'
    assert kwargs.get('reply_markup') is not None, 'служебный ответ без действия-выхода (B7)'
    dm.process_message.assert_not_awaited()


# === 4. Кнопочный путь не зависит от in-memory признака (риск 4 бэклога) ===


@pytest.mark.parametrize('data,expected_await_calls', [
    (f'{_PICK}устал', 0),
    (f'{_PICK}адреналин', 0),
    ('mood:start', 1),
    ('menu:mood', 1),
])
def test_awaiting_mood_flag_only_on_prompt_path(monkeypatch, data: str, expected_await_calls: int):
    """Признак `_awaiting_mood` ставится только при доставке приглашения."""
    dm = _stub_pipeline(monkeypatch)
    marker = AsyncMock()
    monkeypatch.setattr(telegram_bot, '_mark_awaiting_mood', marker)

    _dispatch(data)

    assert marker.await_count == expected_await_calls
    session = dm.session_manager.get_session(TG_USER_ID)
    if expected_await_calls == 0:
        assert AWAITING_MOOD_KEY not in session.last_params


# === 5. Неизвестный ключ — штатный ответ B7 (design.md D6) ===


def test_unknown_mood_key_is_friendly(monkeypatch, caplog):
    """`mood:pick:несуществует` — объяснение с кнопками выхода, без dead-end."""
    dm = _stub_pipeline(monkeypatch)

    with caplog.at_level(logging.WARNING):
        update = _dispatch(f'{_PICK}несуществует')

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_UNKNOWN_CALLBACK
    assert _callbacks(kwargs['reply_markup']) == ['back:list', RANDOM_MOVIE_CALLBACK]
    assert update.callback_query.answer_calls == 1
    assert f'{_PICK}несуществует' in caplog.text
    dm.process_message.assert_not_awaited()


def test_key_with_empty_phrases_is_friendly(monkeypatch, caplog):
    """Пустой список фраз у ключа не даёт IndexError — тот же ответ B7."""
    dm = make_manager()
    dm.mood_triggers = {'устал': []}  # type: ignore[assignment]
    install_callback_mocks(monkeypatch, dm)

    with caplog.at_level(logging.WARNING):
        update = _dispatch(f'{_PICK}устал')

    assert update.callback_query.message.texts[0][0] == telegram_bot._ERROR_TEXT_UNKNOWN_CALLBACK
    assert 'устал' in caplog.text


# === 6. Сбой пайплайна и повтор ТОЙ ЖЕ операции (design.md D6) ===


def test_mood_pick_failure_offers_retry_of_same_key(monkeypatch):
    """Сбой подбора — классифицированная ошибка с целью `retry:mood:pick:{ключ}`."""
    dm = make_manager()
    dm.process_message = AsyncMock(side_effect=NetworkError('нет связи'))  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)

    update = _dispatch(f'{_PICK}устал')

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_NETWORK
    assert f'retry:{_PICK}устал' in _callbacks(kwargs['reply_markup'])


def test_retry_mood_pick_repeats_same_selection(monkeypatch):
    """`retry:mood:pick:устал` через диспетчер снова запускает тот же подбор."""
    dm = _stub_pipeline(monkeypatch)

    update = _dispatch(f'retry:{_PICK}устал')

    dm.process_message.assert_awaited_once()
    assert dm.process_message.await_args.args[2] == _MOOD_TRIGGERS['устал'][0]
    assert not [t for t in _sent_texts(update) if t in _ERROR_TEXTS], (
        'повтор ушёл в «неизвестная цель повтора» вместо подбора'
    )


# === 7. Инварианты словарей (design.md D1, дельта-спека mood-prefilter) ===


def test_button_label_keys_match_mood_triggers():
    """Ключи словаря подписей == ключи `mood_triggers` (рассинхрон = падение).

    T10: словарь подписей `MOOD_BUTTON_LABELS` перенесён в
    dialogue_manager.py (единый источник); тройной инвариант ключей —
    в tests/test_single_source_mood_genre_t10.py.
    """
    dm = make_manager()

    assert set(MOOD_BUTTON_LABELS) == set(dm.mood_triggers)
    assert len(MOOD_BUTTON_LABELS) == len(_MOOD_KEYS) == 8


@pytest.mark.parametrize('mood_key', _MOOD_KEYS)
def test_first_trigger_phrase_is_prefilter_deterministic(mood_key: str):
    """Первая фраза каждого ключа распознаётся префильтром B6 ЭТИМ ЖЕ ключом.

    На этом инварианте держится детерминированность кнопочного пути: если
    первая фраза станет многословной или перестанет совпадать с основой
    словоформы, тап кнопки уйдёт в LLM-классификатор (токены и латентность).
    """
    dm = make_manager()

    assert dm._match_mood_prefilter(_MOOD_TRIGGERS[mood_key][0]) == mood_key


# === 8. Отсутствие коллизий префиксов (design.md D3) ===


def test_route_table_keeps_single_mood_prefix():
    """Новый префикс в `_CALLBACK_ROUTES` не добавлен, `mood:` ровно один."""
    prefixes = [prefix for prefix, _ in telegram_bot._CALLBACK_ROUTES]

    assert prefixes.count('mood:') == 1
    assert _PICK not in prefixes
    assert not any(prefix.startswith('mood:') for prefix in prefixes if prefix != 'mood:')


@pytest.mark.parametrize('data', [f'{_PICK}устал', 'mood:start'])
def test_mood_data_matches_only_mood_route(data: str):
    """`mood:pick:…` и `mood:start` попадают в один и тот же маршрут `mood:`."""
    matched = [prefix for prefix, _ in telegram_bot._CALLBACK_ROUTES if data.startswith(prefix)]

    assert matched == ['mood:']


def test_mood_start_still_shows_prompt(monkeypatch):
    """Регресс: сегмент `mood:start` не перехвачен веткой выбора настроения."""
    dm = _stub_pipeline(monkeypatch)

    update = _dispatch('mood:start')

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._MOOD_PROMPT_HTML
    assert len(_mood_buttons(kwargs['reply_markup'])) == len(_MOOD_KEYS)
    dm.process_message.assert_not_awaited()
