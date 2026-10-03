"""Тесты B5 (bugfix-бэклог): веб-канал Flask после фикса B1 не регрессировал.

Задача B5 бэклога `backlog/backlog_2026-09-25_bugfix_telegram.md`,
изменение `verify-web-channel-b5` (спека `web-chat-channel`). Суффикс
`bf_b5` в имени отличает модуль от задач СТАРОГО бэклога с тем же
номером (`test_feedback_b5.py`, `test_prompt_prefix_b5.py` уже заняты).

Контракт веб-канала (design.md, решения 3-6; сужен изменением B6
`add-mood-prefilter-b6`):
- веб вызывает тот же `DialogueManager.process_message`, но БЕЗ признака
  ожидания настроения `_awaiting_mood` (его ставят только точки
  Telegram-бота); короткие ТОЧНЫЕ совпадения со словарём настроений
  («устал») с B6 распознаёт детерминированный префильтр без LLM
  (покрытие — `tests/test_mood_prefilter_b6.py`), а не покрытые
  словарём mood-запросы идут через LLM-классификацию, где усиленный
  промпт B1 даёт подборку вместо отказа;
- признак `_awaiting_mood` не появляется в сессии веб-пользователя, и
  исходник `src/app.py` не ссылается на него ни в одной точке;
- промпт-страховка веба: `parameter_extraction_prompt.txt` содержит
  правило про односложные настроения и few-shot «устал» → initial+mood;
- smoke веб-маршрутов: `GET /health` (200 healthy / 503 unhealthy),
  `POST /chat` (200 с `response`, 400 на пустое сообщение и пустой JSON);
- офтопик/атака в веб-канале блокируются precheck'ом, как и в боте.

Все проверки офлайн: LLM и kino-API мокируются, PostgreSQL не требуется
(счётчик сессий и статистика подменены); импорт `src/app.py` выполняется
один раз за прогон с сохранением/восстановлением cwd (app.py делает chdir
в src/ на уровне модуля).
"""
import importlib
import os
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from conftest import ROOT, make_list_movies, make_manager, run_coro
from dialogue_manager import AWAITING_MOOD_KEY
from guardrails import REFUSAL_OFFTOPIC

USER_ID = 'web-user-1'


def _web_model_manager(classifier_response: Dict[str, Any]):
    """DialogueManager как модель веб-канала: сессия БЕЗ `_awaiting_mood`.

    `src/app.py` вызывает `process_message` напрямую и признак ожидания
    настроения не устанавливает — этот менеджер воспроизводит ровно такое
    состояние (в отличие от `tests/test_mood_offtopic_b1.py`, где сессия
    создаётся С признаком). LLM и kino-API мокируются.
    """
    dm = make_manager()
    dm.intent_classifier.classify_with_llm = AsyncMock(return_value=classifier_response)
    dm.movie_agent.recommend_movies = AsyncMock(return_value=make_list_movies(3))
    return dm


# === 1. Mood-запрос без признака — веб-пайплайн (критерий приёмки B5) ===


def test_tired_without_flag_uses_llm_and_returns_movies():
    """«я так устал» без `_awaiting_mood`: классификатор вызван, ответ — подборка.

    Модель веб-канала: детерминированный mood-маршрут B1 не срабатывает
    (нет признака), а префильтр B6 ловит только ТОЧНЫЕ совпадения со
    словарём (голое «устал» — см. `tests/test_mood_prefilter_b6.py`),
    поэтому сообщение уходит через `classify_with_llm`; усиленный
    промпт (п.3 B1) классифицирует настроение как initial+mood —
    пользователь получает подборку, а не `REFUSAL_OFFTOPIC`.
    """
    dm = _web_model_manager({'intent': 'initial', 'mood': 'лёгкий', 'movie_type': 'movie'})

    result = run_coro(dm.process_message(None, USER_ID, 'я так устал'))

    # Не покрытое словарём точно — значит через LLM-классификацию (контракт B6)
    dm.intent_classifier.classify_with_llm.assert_awaited_once()
    assert result['needs_clarification'] is False
    assert result['movies_list'], 'веб-пользователь остался без подборки'
    assert result['response'] != REFUSAL_OFFTOPIC
    # Подборка — по жанрам настроения (тот же словарь, что и в боте)
    genres = [c.kwargs['genre_name'] for c in dm.movie_agent.recommend_movies.await_args_list]
    assert genres[0] in dm.mood_to_genre['устал']


def test_web_session_never_gets_awaiting_mood_flag():
    """После обработки веб-сообщения признака `_awaiting_mood` в сессии нет."""
    dm = _web_model_manager({'intent': 'initial', 'mood': 'лёгкий'})

    run_coro(dm.process_message(None, USER_ID, 'устал'))

    session = dm.session_manager.get_session(USER_ID)
    assert AWAITING_MOOD_KEY not in session.last_params
    # Сессия сохранена штатно (веб-канал не отличается от бота по хранению)
    assert USER_ID in dm.session_manager.saved


# === 2. Офтопик/атака в веб-канале блокируются как и в боте ===


def test_offtopic_attack_in_web_channel_is_refused_by_precheck():
    """«напиши код» без признака: отказ из precheck, без классификатора и поиска.

    Единственный веб-кейс guardrails (не дублирует `test_mood_offtopic_b1.py` —
    там тот же сценарий С признаком ожидания настроения, и `test_guardrails.py` —
    там проверяется сам `precheck_message`).
    """
    dm = _web_model_manager({'intent': 'initial'})

    result = run_coro(dm.process_message(None, USER_ID, 'напиши код на python'))

    assert result['response'] == REFUSAL_OFFTOPIC
    assert result['needs_clarification'] is False
    assert 'movies_list' not in result
    dm.intent_classifier.classify_with_llm.assert_not_awaited()
    dm.movie_agent.recommend_movies.assert_not_awaited()


# === 3. Статические гарантии: app.py не знает про признак, промпт усилен ===


@pytest.mark.parametrize('marker', ['AWAITING_MOOD_KEY', '_awaiting_mood', '_mark_awaiting_mood'])
def test_app_source_does_not_reference_awaiting_mood(marker: str):
    """Веб-канал не устанавливает признак ожидания настроения (design.md, решение 5).

    Признак пишут только точки Telegram-бота (`_mark_awaiting_mood` в
    `telegram_bot.py`); появление любого из маркеров в `src/app.py`
    означало бы изменение контракта веб-канала.
    """
    with open(os.path.join(ROOT, 'src', 'app.py'), encoding='utf-8') as f:
        source = f.read()

    assert marker not in source


def test_extraction_prompt_keeps_mood_safeguard_for_web():
    """Промпт-страховка веба: правило про односложные настроения и few-shot «устал».

    Префильтр B6 закрывает только точные совпадения со словарём; прочие
    mood-запросы веб-канала («я так устал», развёрнутые формулировки)
    зависят от LLM-классификации, поэтому усиление промпта из п.3 B1 —
    их защита от ложного `offtopic`.
    """
    path = os.path.join(ROOT, 'src', 'prompts', 'parameter_extraction_prompt.txt')
    with open(path, encoding='utf-8') as f:
        prompt = f.read()

    # Правило: односложные состояния-настроения не считаются офтопиком
    assert 'НЕ считается offtopic' in prompt
    # Few-shot пример: «устал» → initial + mood (строка ответа идёт сразу за запросом)
    assert 'Запрос: "устал"' in prompt
    tired_answer = prompt.split('Запрос: "устал"')[1].split('\n')[1]
    assert '"intent":"initial"' in tired_answer
    assert '"mood":"лёгкий"' in tired_answer
    assert '"offtopic"' not in tired_answer


# === 4. Smoke веб-маршрутов через Flask test client ===


@pytest.fixture(scope='session')
def web_app():
    """Одноразовый импорт `src/app.py` с сохранением cwd (design.md, решение 2).

    app.py на уровне модуля делает `os.chdir(src/)` — без восстановления
    cwd «уехал» бы на весь остаток pytest-прогона. Повторный импорт не
    исполняется (модуль кэшируется в sys.modules).
    """
    saved_cwd = os.getcwd()
    try:
        module = importlib.import_module('app')
    finally:
        os.chdir(saved_cwd)
    return module


@pytest.fixture()
def client(web_app, monkeypatch):
    """Flask test client без PostgreSQL и внешних вызовов (design.md, решение 3).

    Подменяются только точки, смотрящие наружу: клиентская статистика
    (пишет в БД) и счётчик сессий `/health` (читает БД). Маршруты,
    валидация, сессионная кука `user_id` и executor работают по-настоящему.
    """
    monkeypatch.setattr(web_app, 'track_client_request', lambda *args, **kwargs: None)
    monkeypatch.setattr(web_app.session_manager, 'count_sessions', lambda: 0)
    web_app.app.config['TESTING'] = True
    return web_app.app.test_client()


def test_health_returns_200_healthy(web_app, client, monkeypatch):
    """`GET /health`: kino-API доступно → 200 и статус `healthy`."""
    monkeypatch.setattr(web_app.dialogue_manager.movie_agent, 'health_check', AsyncMock(return_value=True))

    response = client.get('/health')

    assert response.status_code == 200
    data = response.get_json()
    assert data['status'] == 'healthy'
    assert data['sessions_count'] == 0


def test_health_returns_503_unhealthy(web_app, client, monkeypatch):
    """`GET /health`: kino-API недоступно → 503 и статус `unhealthy`."""
    monkeypatch.setattr(web_app.dialogue_manager.movie_agent, 'health_check', AsyncMock(return_value=False))

    response = client.get('/health')

    assert response.status_code == 503
    assert response.get_json()['status'] == 'unhealthy'


def test_chat_valid_message_returns_200(web_app, client, monkeypatch):
    """`POST /chat` с валидным JSON → 200 с `response` из диалогового ядра."""
    process_message = AsyncMock(return_value={'response': 'Вот подборка фильмов', 'needs_clarification': False})
    monkeypatch.setattr(web_app.dialogue_manager, 'process_message', process_message)

    response = client.post('/chat', json={'message': 'привет'})

    assert response.status_code == 200
    data = response.get_json()
    assert data['response'] == 'Вот подборка фильмов'
    assert data['needs_clarification'] is False
    # Ядро вызвано с текстом сообщения; веб не передаёт никаких служебных признаков
    process_message.assert_awaited_once()
    _, user_id, message = process_message.await_args.args
    assert message == 'привет'
    assert isinstance(user_id, str) and user_id


def test_chat_empty_message_returns_400(client):
    """`POST /chat` с пустым/пробельным сообщением → 400, обработка не запускается."""
    response = client.post('/chat', json={'message': '   '})

    assert response.status_code == 400
    assert response.get_json()['error'] == 'Сообщение не может быть пустым'


def test_chat_without_json_returns_400(client):
    """`POST /chat` без JSON-данных (пустой объект) → 400 с описанием ошибки."""
    response = client.post('/chat', json={})

    assert response.status_code == 400
    assert response.get_json()['error'] == 'No JSON data provided'
