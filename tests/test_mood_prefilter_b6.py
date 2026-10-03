"""Тесты B6: детерминированный mood-префильтр до LLM для коротких сообщений.

Изменение `add-mood-prefilter-b6` (бэклог
`backlog/backlog_2026-09-25_bugfix_telegram.md`, задача B6, P2; зависит
от B1/B4 — словарь `mood_triggers` и маршрут `_awaiting_mood` уже есть).

Покрытие (design.md D1–D7):
- короткое (≤ `MOOD_PREFILTER_MAX_WORDS` слов) ТОЧНОЕ совпадение со
  словарём («устал», «грустно», «скучно», словоформы, «ё»/«е») даёт
  подборку БЕЗ вызова `classify_with_llm` — в любом канале и БЕЗ
  признака `_awaiting_mood`;
- консервативность: «устал от жизни» и длинные сообщения уходят в
  обычный LLM-пайплайн (префильтр молчит);
- `precheck_message` приоритетнее префильтра: атака/офтопик в коротком
  сообщении блокируются ДО сопоставления со словарём (spy не вызван);
- порядок веток D1: при активном признаке срабатывает маршрут B1
  (прежнее логирование), после израсходованного признака то же
  сообщение ловит префильтр B6;
- unit-проверки хелперов `_normalize_mood_text` и
  `_match_mood_prefilter`, конфиг-константа порога.

Все проверки офлайн: in-memory заглушка менеджера сессий из
`tests/conftest.py`; LLM и Kinopoisk мокируются, Telegram не импортируется.
"""
import asyncio
import logging
from typing import Any, Optional
from unittest.mock import AsyncMock, Mock

import pytest

from conftest import make_list_movies, make_manager
from dialogue_manager import (
    AWAITING_MOOD_KEY,
    MOOD_ANSWER_MAX_WORDS,
    MOOD_PREFILTER_MAX_WORDS,
    DialogueManager,
    _normalize_mood_text,
)
from guardrails import REFUSAL_OFFTOPIC

USER_ID = 'u1'


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _dm_without_flag() -> DialogueManager:
    """DialogueManager БЕЗ признака `_awaiting_mood` (модель веб-канала
    или обычного сообщения бота).

    Классификатор мокируется ответом `offtopic`: если префильтр сработал,
    вызова не было вовсе (`assert_not_awaited`) и пользователь получил
    подборку; если сообщение ушло обычным пайплайном — отказ из мока.
    """
    dm = make_manager()
    dm.intent_classifier.classify_with_llm = AsyncMock(return_value={'intent': 'offtopic'})
    dm.movie_agent.recommend_movies = AsyncMock(return_value=make_list_movies(3))
    return dm


def _dm_with_mood_flag() -> DialogueManager:
    """DialogueManager с признаком `_awaiting_mood` в сессии `USER_ID` (B1)."""
    dm = _dm_without_flag()
    dm.session_manager.get_session(USER_ID).last_params[AWAITING_MOOD_KEY] = True
    return dm


# === 1. Критерий приёмки: точное совпадение — подборка без LLM ===


@pytest.mark.parametrize(
    'message,mood_key',
    [
        ('устал', 'устал'),
        ('грустно', 'грустн'),
        ('скучно', 'скучно'),
        ('Грустно!', 'грустн'),        # пунктуация и регистр снимаются нормализацией
        ('скучно...', 'скучно'),
        ('весёлое', 'весел'),           # словоформа основы + «ё»
        ('веселое', 'весел'),           # то же через «е»
        ('тихий вечер', 'устал'),       # многословная фраза словаря — точное равенство
    ],
)
def test_short_exact_mood_match_returns_movies_without_llm(message: str, mood_key: str):
    """Короткое точное совпадение со словарём — подборка, классификатор не вызван."""
    dm = _dm_without_flag()

    result = _run(dm.process_message(None, USER_ID, message))

    dm.intent_classifier.classify_with_llm.assert_not_awaited()
    assert result['needs_clarification'] is False
    assert result['movies_list'], f'подборка пустая для «{message}»'
    assert result['response'] != REFUSAL_OFFTOPIC
    # Жанры — из единого словаря `mood_to_genre` для распознанного ключа
    genres = [c.kwargs['genre_name'] for c in dm.movie_agent.recommend_movies.await_args_list]
    assert genres[0] == dm.mood_to_genre[mood_key][0]
    assert set(genres) == set(dm.mood_to_genre[mood_key])


def test_prefilter_hit_is_logged_in_russian_without_llm(caplog):
    """Срабатывание префильтра логируется по-русски с пометкой «без LLM» (D6)."""
    dm = _dm_without_flag()

    with caplog.at_level(logging.INFO):
        _run(dm.process_message(None, USER_ID, 'устал'))

    assert "mood='устал'" in caplog.text
    assert 'без LLM-классификатора' in caplog.text
    assert 'префильтр' in caplog.text
    assert f'user_id={USER_ID}' in caplog.text


# === 2. Консервативность: неоднозначные и длинные сообщения — LLM-пайплайн ===


def test_tired_of_life_goes_to_llm_pipeline():
    """«устал от жизни» (3 слова, НЕ точное совпадение) — обычный пайплайн.

    Риск задачи B6: детерминированный слой не должен трактовать
    неоднозначный ввод — сообщение проходит порог по длине, но правило
    словоформы применяется только к ОДИНОЧНОМУ слову (D3).
    """
    dm = _dm_without_flag()
    message = 'устал от жизни'
    assert len(message.split()) <= MOOD_PREFILTER_MAX_WORDS

    result = _run(dm.process_message(None, USER_ID, message))

    dm.intent_classifier.classify_with_llm.assert_awaited_once()
    assert result['response'] == REFUSAL_OFFTOPIC  # ответ мока обработан штатно
    dm.movie_agent.recommend_movies.assert_not_awaited()


def test_long_message_with_mood_words_goes_to_llm_pipeline():
    """Сообщение длиннее порога префильтра — обычная LLM-классификация."""
    dm = _dm_without_flag()
    message = 'устал от всего и хочу посмотреть что-нибудь про космос с высоким рейтингом'
    assert len(message.split()) > MOOD_PREFILTER_MAX_WORDS

    _run(dm.process_message(None, USER_ID, message))

    dm.intent_classifier.classify_with_llm.assert_awaited_once()


@pytest.mark.parametrize('message', ['хочу адреналина', 'привет', 'посоветуй комедию пожалуйста'])
def test_non_exact_short_messages_go_to_llm_pipeline(message: str):
    """Короткие сообщения вне точного совпадения со словарём — LLM-пайплайн."""
    dm = _dm_without_flag()

    _run(dm.process_message(None, USER_ID, message))

    dm.intent_classifier.classify_with_llm.assert_awaited_once()


# === 3. Защита приоритетнее префильтра (precheck ДО словаря) ===


@pytest.mark.parametrize(
    'message',
    [
        'напиши код',       # явный офтопик-маркер, 2 слова — порог длины пройден
        'войди в роль',     # атака на промпт, ровно 3 слова — граница порога
    ],
)
def test_blocked_short_message_never_reaches_prefilter(message: str):
    """Заблокированное precheck короткое сообщение не доходит до префильтра."""
    dm = _dm_without_flag()
    spy = Mock(wraps=dm._match_mood_prefilter)
    dm._match_mood_prefilter = spy

    result = _run(dm.process_message(None, USER_ID, message))

    assert result['response'] == REFUSAL_OFFTOPIC
    assert result['needs_clarification'] is False
    assert 'movies_list' not in result
    spy.assert_not_called()
    dm.intent_classifier.classify_with_llm.assert_not_awaited()
    dm.movie_agent.recommend_movies.assert_not_awaited()


# === 4. Порядок веток: awaiting_mood (B1) → префильтр (B6) → LLM (D1) ===


def test_awaiting_mood_branch_keeps_priority_and_own_log(caplog):
    """При активном признаке срабатывает маршрут B1, а не префильтр B6."""
    dm = _dm_with_mood_flag()

    with caplog.at_level(logging.INFO):
        result = _run(dm.process_message(None, USER_ID, 'устал'))

    assert result['movies_list']
    dm.intent_classifier.classify_with_llm.assert_not_awaited()
    # Логирование ветки B1 сохранено, формулировка префильтра B6 не появилась
    assert 'Ответ на приглашение о настроении' in caplog.text
    assert 'префильтр' not in caplog.text


def test_second_short_mood_after_flag_consumed_uses_prefilter(caplog):
    """Признак израсходован — следующее короткое настроение ловит префильтр."""
    dm = _dm_with_mood_flag()

    _run(dm.process_message(None, USER_ID, 'устал'))
    with caplog.at_level(logging.INFO):
        second = _run(dm.process_message(None, USER_ID, 'устал'))

    assert second['movies_list']
    assert 'префильтр' in caplog.text
    # Оба сообщения обработаны детерминированно — LLM не вызывался ни разу
    dm.intent_classifier.classify_with_llm.assert_not_awaited()


def test_flag_set_but_non_mood_message_still_uses_llm():
    """Признак активен, но сообщение не про настроение — обычный пайплайн.

    Префильтр применяется, когда ветка B1 «не сработала» (D1): здесь не
    сработал ни маршрут ожидания, ни префильтр — вызван классификатор.
    """
    dm = _dm_with_mood_flag()

    _run(dm.process_message(None, USER_ID, 'привет'))

    dm.intent_classifier.classify_with_llm.assert_awaited_once()


# === 5. Unit-уровень: хелперы и конфиг-константы ===


def test_prefilter_threshold_is_config_constant():
    """Порог префильтра — конфиг-константа 3 слова, уже порога ветки B1."""
    assert MOOD_PREFILTER_MAX_WORDS == 3
    assert MOOD_PREFILTER_MAX_WORDS < MOOD_ANSWER_MAX_WORDS


@pytest.mark.parametrize(
    'raw,expected',
    [
        ('Устал!!!', 'устал'),
        ('ТиХий, ВеЧер…', 'тихий вечер'),   # пунктуация снята, пробелы схлопнуты
        ('весёлое', 'веселое'),              # «ё» → «е»
        ('  несколько   пробелов ', 'несколько пробелов'),
        ('', ''),
        ('   ', ''),
    ],
)
def test_normalize_mood_text(raw: str, expected: str):
    """Нормализация D2: lower + снятие пунктуации + «ё»→«е» + пробелы."""
    assert _normalize_mood_text(raw) == expected


@pytest.mark.parametrize(
    'message,expected',
    [
        # Точное равенство с фразой словаря (одно- и многословные)
        ('устал', 'устал'),
        ('скучно', 'скучно'),
        ('хочу радости', 'грустн'),
        ('тихий вечер', 'устал'),
        # Словоформа одиночного слова (основа без пробелов)
        ('грустно', 'грустн'),
        ('усталость', 'устал'),
        ('весёлое', 'весел'),
        ('тоска', 'грустн'),
        ('хандра', 'грустн'),
        ('романтическое', 'романт'),
        ('страшно', 'страшн'),
        # Консервативность: не точные совпадения и длинные сообщения
        ('устал от жизни', None),
        ('хочу адреналина', None),
        ('привет', None),
        ('напиши код', None),
        ('устал очень сильно сегодня', None),
        ('', None),
        ('...', None),
    ],
)
def test_match_mood_prefilter(message: str, expected: Optional[str]):
    """Хелпер сопоставления D3: ключ настроения либо None."""
    dm = make_manager()

    assert dm._match_mood_prefilter(message) == expected


def test_prefilter_matches_only_dictionary_sources():
    """Префильтр не вводит новый словарь: каждая фраза — из `mood_triggers`."""
    dm = make_manager()
    all_phrases = {_normalize_mood_text(p) for phrases in dm.mood_triggers.values() for p in phrases}

    for message in ('устал', 'грустно', 'весёлое', 'тихий вечер', 'хочу радости'):
        assert dm._match_mood_prefilter(message) is not None, message
    # Распознанное сообщение либо точно равно фразе, либо словоформа основы
    for message in ('устал', 'тихий вечер', 'хочу радости'):
        assert _normalize_mood_text(message) in all_phrases
