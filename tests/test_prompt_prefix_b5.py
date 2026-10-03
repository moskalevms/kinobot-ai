"""Регрессионные тесты стабильного префикса LLM-запросов (задача B5,
изменение compress-prompts-stable-prefix).

Префиксное кэширование у любого провайдера (отчёт
docs/research/token-economy-llm-2026-09-18.md §3.1, §5.1, §6) работает
только если начало запроса побайтово идентично между вызовами. Проверяем:

- sha256 статического системного префикса одинаков между двумя сборками
  сообщений с РАЗНЫМИ входными данными (текст пользователя, история,
  параметры сессии) — на уровне сборки (в т.ч. для двух разных свежих
  экземпляров классификатора) и на уровне полного вызова
  classify_with_llm с фейковым роутером;
- префикс — строго первое сообщение (role=system), динамический контент
  (запрос пользователя) — строго после него (role=user), внутри явной
  разметки <user_query>;
- префикс содержит не менее 1024 токенов (оценка: ~4 байта на токен —
  соотношение из отчёта, 11 665 байт ≈ 3K токенов);
- кэш промпта: повторная сборка не читает файл заново и возвращает тот
  же объект str.

Тесты НЕ требуют сети, API-ключей и .env: LLM-роутер — None либо фейк
(ключи в conftest.py фиктивные, сетевых вызовов нет).
"""
import hashlib
import os
from typing import Any, Dict, List

from conftest import run_coro
from intent_classifier import IntentClassifier

PROMPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'src', 'prompts'
)

# Минимальный размер кэшируемого префикса — 1024 токенов (порог
# префиксного кэширования провайдеров, отчёт §5.1/§6).
MIN_PREFIX_TOKENS = 1024


def _classifier(router: Any = None) -> IntentClassifier:
    """Классификатор с реальными промптами и без сети."""
    return IntentClassifier(llm_router=router, prompts_dir=PROMPTS_DIR)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _est_tokens(text: str) -> int:
    """Консервативная оценка токенов: ~4 байта на токен (отчёт §6)."""
    return len(text.encode('utf-8')) // 4


class FakeRouter:
    """Фейк LLMRouter: записывает сообщения каждого вызова, возвращает
    валидный JSON-ответ классификатора. Сети нет."""

    def __init__(self) -> None:
        self.captured: List[List[Dict[str, str]]] = []

    async def call_llm(self, session: Any, messages: List[Dict[str, str]], max_tokens: int = 500) -> str:
        self.captured.append(messages)
        return '{"intent":"initial","genre":"комедия","movie_type":"movie","critics_approved":false}'


def test_prefix_hash_stable_between_different_builds() -> None:
    """sha256 системного префикса одинаков при разных входах сборки."""
    clf = _classifier()
    messages_a = clf.build_classification_messages(
        'посоветуй комедию', {'last_movies': [], 'last_params': {}}
    )
    messages_b = clf.build_classification_messages(
        'топ триллеров 2020-х от критиков, что-то похожее на Начало',
        {'last_movies': [{'title': 'Начало', 'year': 2010}], 'last_params': {'genre': 'драма', 'year': 1990}},
    )
    assert _sha256(messages_a[0]['content']) == _sha256(messages_b[0]['content'])
    # Динамика действительно разная — иначе тест ничего не проверял бы.
    assert messages_a[1]['content'] != messages_b[1]['content']


def test_prefix_hash_stable_across_fresh_instances() -> None:
    """sha256 префикса одинаков для ДВУХ РАЗНЫХ свежих экземпляров
    IntentClassifier: каждый самостоятельно читает промпт с диска, и оба
    результата побайтово идентичны при разных входных данных — префиксное
    кэширование у провайдера переживает пересоздание классификатора
    (рестарт процесса/новый воркер)."""
    clf_a = _classifier()
    clf_b = _classifier()
    assert clf_a is not clf_b
    messages_a = clf_a.build_classification_messages(
        'посоветуй комедию', {'last_movies': [], 'last_params': {}}
    )
    messages_b = clf_b.build_classification_messages(
        'мрачный пеплум про гладиатора из топ-50',
        {'last_movies': [{'title': 'Дюна', 'year': 2021}], 'last_params': {'genre': 'драма', 'count': 50}},
    )
    assert _sha256(messages_a[0]['content']) == _sha256(messages_b[0]['content'])
    # Динамика действительно разная — иначе тест ничего не проверял бы.
    assert messages_a[1]['content'] != messages_b[1]['content']


def test_prefix_is_first_message_and_dynamic_after() -> None:
    """Статика — строго первое сообщение (system), запрос пользователя —
    строго после префикса (user), внутри разметки <user_query>."""
    clf = _classifier()
    # Запрос намеренно уникален (не совпадает с few-shot примерами промпта).
    query = 'мистический триллер про смотрителя маяка'
    messages = clf.build_classification_messages(query)
    assert [m['role'] for m in messages] == ['system', 'user']
    assert '<user_query>' in messages[1]['content']
    assert query in messages[1]['content']
    # Пользовательский ввод не проникает в системный префикс.
    assert query not in messages[0]['content']


def test_prefix_length_at_least_1024_tokens() -> None:
    """Префикс достаточно длинный, чтобы провайдеру было что кэшировать."""
    clf = _classifier()
    prefix = clf.build_classification_messages('боевик')[0]['content']
    assert _est_tokens(prefix) >= MIN_PREFIX_TOKENS, (
        f'Префикс {_est_tokens(prefix)} токенов (оценка) < {MIN_PREFIX_TOKENS}'
    )


def test_prefix_stable_through_full_classify_calls() -> None:
    """Полный цикл classify_with_llm с фейковым роутером: системное
    сообщение, фактически отправленное в LLM, побайтово идентично между
    двумя вызовами с разными входами."""
    router = FakeRouter()
    clf = _classifier(router)
    params_a = run_coro(clf.classify_with_llm(None, 'посоветуй лёгкую комедию', {}))
    params_b = run_coro(clf.classify_with_llm(
        None, 'расскажи о фильме Начало', {'last_movies': [{'title': 'Дюна'}], 'last_params': {'count': 5}}
    ))
    assert len(router.captured) == 2
    first_call, second_call = router.captured
    assert _sha256(first_call[0]['content']) == _sha256(second_call[0]['content'])
    assert first_call[0]['role'] == 'system' and second_call[0]['role'] == 'system'
    # Классификация при этом работает: ответы LLM разобраны.
    assert params_a['intent'] == 'initial' and params_b['intent'] == 'initial'


def test_system_prompt_cached_same_object() -> None:
    """Кэш B5: после первой загрузки возвращается тот же объект str,
    повторного чтения файла нет."""
    clf = _classifier()
    first = clf.build_classification_messages('драма')[0]['content']
    second = clf.build_classification_messages('комедия')[0]['content']
    # Идентичность объекта str доказывает кэширование: повторное чтение
    # файла создало бы новый объект (равный по содержимому, но не тот же).
    # Приватное состояние (_system_prompt_cache) тест не проверяет.
    assert first is second
