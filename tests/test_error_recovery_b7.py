"""Тесты B7: дружелюбные ошибки с выходом (add-onboarding-and-error-recovery).

Без сети: классификация ошибок (сеть/Kinopoisk, LLM/классификация, прочее),
текст и ≥1 действие-кнопка для каждого класса, отсутствие генерика
«⚠️ Произошла ошибка» без кнопок, сохранённое логирование и маршрут
повтора `retry:*` (last/top/random/mood/info:{id}/similar:{id}/alt/back,
потеря контекста), разделение не-200 ответов Kinopoisk на временные
(429/5xx — с повтором) и постоянные (прочие 4xx — выходы без повтора),
отсутствие дубля действия «🎲» при цели повтора `random` и обрезка
пользовательского текста в логах.
Мок-паттерны — общие фейки из `tests/conftest.py`.
"""
import asyncio
import logging
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.error import NetworkError, TimedOut

import telegram_bot
from conftest import (
    FakeCallbackUpdate,
    FakeChat,
    FakeMessage,
    FakeUser,
    all_buttons,
    editable_message,
    install_callback_mocks,
    make_card_result,
    make_list_result,
    make_manager,
    make_movie,
)


def _run(coro):
    return asyncio.run(coro)


def _callbacks(markup: Any) -> list:
    return [b.callback_data for b in all_buttons(markup)]


# --- 1. Классификация ошибок ---


def test_classify_network_errors():
    """Сеть/Kinopoisk: aiohttp-ошибки, таймауты, ошибки доставки Telegram."""
    assert telegram_bot.classify_error(aiohttp.ClientError('соединение сброшено')) == 'network'
    assert telegram_bot.classify_error(asyncio.TimeoutError()) == 'network'
    assert telegram_bot.classify_error(NetworkError('Telegram недоступен')) == 'network'
    assert telegram_bot.classify_error(TimedOut()) == 'network'
    # Пустой результат без исключения (движок проглотил сбой API) — тоже сеть
    assert telegram_bot.classify_error(None) == 'network'


def test_classify_llm_errors():
    """LLM/классификация: маркеры провайдеров в тексте исключения."""
    assert telegram_bot.classify_error(Exception('Ошибка GigaChat API: HTTP 500, {}')) == 'llm'
    assert telegram_bot.classify_error(Exception('Ошибка получения токена: HTTP 401')) == 'llm'
    assert telegram_bot.classify_error(Exception('DeepSeek недоступен')) == 'llm'


def test_classify_generic_error():
    """Прочие исключения — резервный класс (тоже с кнопками)."""
    assert telegram_bot.classify_error(ValueError('странные данные')) == 'generic'
    assert telegram_bot.classify_error(RuntimeError('всё сломалось')) == 'generic'


def test_llm_transient_marker_does_not_steal_network_errors():
    """aiohttp-сбой Kinopoisk НЕ попадает в класс LLM (design.md D2).

    `gigachat_client.is_transient_error` ловит любые aiohttp-ошибки —
    классификатор опирается на текстовые маркеры, а не на него.
    """
    exc = aiohttp.ClientConnectionError('Kinopoisk недоступен')
    assert telegram_bot.classify_error(exc) == 'network'


# --- 2. Текст и кнопки для каждого класса ---


def test_every_error_class_has_own_text_and_actions():
    """Каждый класс — свой текст И ≥1 кнопка; подписи/данные в лимитах."""
    replies = {
        'network': telegram_bot.build_error_reply(aiohttp.ClientError('x'), 'last'),
        'llm': telegram_bot.build_error_reply(Exception('Ошибка GigaChat API'), None),
        'generic': telegram_bot.build_error_reply(ValueError('x'), None),
    }
    texts = set()
    for error_class, (text, markup) in replies.items():
        assert text, error_class
        texts.add(text)
        buttons = all_buttons(markup)
        assert len(buttons) >= 1, error_class
        for button in buttons:
            assert len(button.text) <= 64
            assert len(button.callback_data.encode('utf-8')) <= 64
    # Классы различимы по тексту
    assert len(texts) == 3
    assert replies['network'][0] == telegram_bot._ERROR_TEXT_NETWORK
    assert replies['llm'][0] == telegram_bot._ERROR_TEXT_LLM
    assert replies['generic'][0] == telegram_bot._ERROR_TEXT_GENERIC


def test_network_reply_offers_retry_and_random():
    """Сетевой класс: «🔄 Повторить» (та же операция) + «🎲 Случайный фильм»."""
    text, markup = telegram_bot.build_error_reply(aiohttp.ClientError('x'), 'last')
    assert text == telegram_bot._ERROR_TEXT_NETWORK
    callbacks = _callbacks(markup)
    assert 'retry:last' in callbacks
    assert telegram_bot.RANDOM_MOVIE_CALLBACK in callbacks


def test_network_reply_with_random_target_has_no_duplicate_action():
    """Цель повтора `random` — второй выход «🏆 Топ комедий», а не дубль «🎲».

    Две кнопки, запускающие одну операцию, нарушают дух гайдлайна B6
    (1 эмодзи = 1 смысл) и не дают нового выхода; ≥1 действие сохранено.
    """
    text, markup = telegram_bot.build_error_reply(aiohttp.ClientError('x'), 'random')
    assert text == telegram_bot._ERROR_TEXT_NETWORK
    callbacks = _callbacks(markup)
    assert 'retry:random' in callbacks
    assert 'retry:top' in callbacks
    assert telegram_bot.RANDOM_MOVIE_CALLBACK not in callbacks
    assert len(all_buttons(markup)) >= 2


def test_generic_reply_with_random_target_has_no_duplicate_action():
    """Резервный класс + цель `random`: дубль «🎲» тоже исключён."""
    _, markup = telegram_bot.build_error_reply(ValueError('x'), 'random')
    assert _callbacks(markup) == ['retry:random', 'retry:top']


def test_exit_keyboard_is_generic_error_keyboard():
    """nit n1: один источник формы кнопок выхода (без дублирования состава)."""
    exit_buttons = [(b.text, b.callback_data) for b in all_buttons(telegram_bot.build_exit_keyboard())]
    generic_buttons = [
        (b.text, b.callback_data)
        for b in all_buttons(telegram_bot.build_error_keyboard(telegram_bot.ERROR_CLASS_GENERIC))
    ]
    assert exit_buttons == generic_buttons
    assert [data for _, data in exit_buttons] == ['retry:top', telegram_bot.RANDOM_MOVIE_CALLBACK]


def test_unknown_callback_keyboard_builder():
    """nit n2: клавиатура неизвестного callback — именованный билдер."""
    markup = telegram_bot.build_unknown_callback_keyboard()

    assert isinstance(markup, InlineKeyboardMarkup)
    buttons = all_buttons(markup)
    assert [(b.text, b.callback_data) for b in buttons] == [
        (telegram_bot._EXIT_BACK_BUTTON_TEXT, 'back:list'),
        (telegram_bot._EXIT_RANDOM_BUTTON_TEXT, telegram_bot.RANDOM_MOVIE_CALLBACK),
    ]
    for button in buttons:
        assert len(button.text) <= 64
        assert len(button.callback_data.encode('utf-8')) <= 64


def test_llm_reply_offers_valid_examples():
    """LLM-класс — 2–3 кнопки с валидными примерами действий."""
    text, markup = telegram_bot.build_error_reply(Exception('GigaChat HTTP 500'), None)
    assert text == telegram_bot._ERROR_TEXT_LLM
    callbacks = _callbacks(markup)
    assert 'retry:top' in callbacks
    assert telegram_bot.RANDOM_MOVIE_CALLBACK in callbacks


def test_no_buttonless_generic_left_in_source():
    """Генерик «⚠️ Произошла ошибка» без кнопок не остался ни в одном пути."""
    source_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src', 'telegram_bot.py')
    with open(source_path, encoding='utf-8') as f:
        source = f.read()
    assert '⚠️ Произошла ошибка' not in source
    assert 'Произошла непредвиденная ошибка' not in source
    assert 'Неизвестная команда.' not in source


# --- 3. Текстовый путь: _process_and_reply ---


def _process_update() -> SimpleNamespace:
    return SimpleNamespace(message=FakeMessage(chat=FakeChat()))


def test_process_and_reply_error_saves_last_query_and_offers_retry(monkeypatch, caplog):
    """Сбой текстового пути: last_query сохранён, ответ — класс + «🔄 Повторить»."""
    dm = make_manager()
    dm.process_message = AsyncMock(side_effect=aiohttp.ClientError('нет связи'))
    install_callback_mocks(monkeypatch, dm)

    update = _process_update()
    context = SimpleNamespace(user_data={})
    with caplog.at_level(logging.ERROR):
        _run(telegram_bot._process_and_reply(update, context, '777', 'топ комедий'))

    assert context.user_data[telegram_bot._LAST_QUERY_KEY] == 'топ комедий'
    text, kwargs = update.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_NETWORK
    assert kwargs.get('parse_mode') == 'HTML'
    assert 'retry:last' in _callbacks(kwargs['reply_markup'])
    # Логирование сохранено в прежнем объёме (error + exc_info)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any('топ комедий' in r.getMessage() for r in errors)
    assert any(r.exc_info for r in errors)


def test_process_and_reply_llm_error_has_examples(monkeypatch):
    """Исключение с маркером LLM — текст «Не получилось понять запрос» + примеры."""
    dm = make_manager()
    dm.process_message = AsyncMock(side_effect=Exception('Ошибка GigaChat API: HTTP 503'))
    install_callback_mocks(monkeypatch, dm)

    update = _process_update()
    context = SimpleNamespace(user_data={})
    _run(telegram_bot._process_and_reply(update, context, '777', 'мне грустно'))

    text, kwargs = update.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_LLM
    callbacks = _callbacks(kwargs['reply_markup'])
    assert 'retry:last' in callbacks
    assert 'retry:top' in callbacks
    assert telegram_bot.RANDOM_MOVIE_CALLBACK in callbacks


# --- 4. error_handler ---


def test_error_handler_classifies_and_adds_buttons(caplog):
    """Глобальный обработчик: классификация context.error + кнопки выхода."""
    update = SimpleNamespace(effective_message=FakeMessage())
    context = SimpleNamespace(error=Exception('Ошибка GigaChat API: HTTP 500'))

    with caplog.at_level(logging.ERROR):
        _run(telegram_bot.error_handler(update, context))

    text, kwargs = update.effective_message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_LLM
    assert kwargs.get('parse_mode') == 'HTML'
    assert len(all_buttons(kwargs['reply_markup'])) >= 1
    # Логирование как раньше
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)


# --- 5. Неизвестный callback и ошибки разбора ---


def test_unknown_callback_explains_and_offers_return():
    """Неизвестный префикс — объяснение + возврат к списку/случайный фильм."""
    update = FakeCallbackUpdate('menu:mood', from_user=FakeUser(777))

    _run(telegram_bot.handle_movie_detail(update, None))

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_UNKNOWN_CALLBACK
    callbacks = _callbacks(kwargs['reply_markup'])
    assert 'back:list' in callbacks
    assert telegram_bot.RANDOM_MOVIE_CALLBACK in callbacks
    assert update.callback_query.answer_calls == 1


def test_bad_callback_id_offers_exit(monkeypatch):
    """Ошибка разбора callback_data — объяснение + действие (не голый текст)."""
    dm = make_manager()
    install_callback_mocks(monkeypatch, dm)

    update = FakeCallbackUpdate('info:abc', from_user=FakeUser(777))
    _run(telegram_bot.handle_movie_detail(update, None))

    text, kwargs = update.callback_query.message.texts[0]
    assert text == 'Не удалось определить фильм.'
    assert telegram_bot.RANDOM_MOVIE_CALLBACK in _callbacks(kwargs['reply_markup'])


# --- 5.1 Не-200 от Kinopoisk: временные и постоянные отказы (minor m3) ---


def test_info_callback_retryable_status_offers_card_retry(monkeypatch, caplog):
    """503 — временный сбой: класс «сеть», повтор ЭТОЙ карточки, warning в лог."""
    dm = make_manager()
    install_callback_mocks(monkeypatch, dm, payload={}, status=503)

    update = FakeCallbackUpdate('info:447301', from_user=FakeUser(777))
    with caplog.at_level(logging.WARNING):
        _run(telegram_bot.handle_movie_detail(update, None))

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_NETWORK
    callbacks = _callbacks(kwargs['reply_markup'])
    assert 'retry:info:447301' in callbacks
    assert telegram_bot.RANDOM_MOVIE_CALLBACK in callbacks
    # Сбой не-200 больше не «молчаливый»: статус и фильм видны в логе
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any('HTTP 503' in m and '447301' in m for m in warnings)


def test_info_callback_rate_limit_status_is_retryable(monkeypatch):
    """429 (лимит запросов) — тоже временный сбой: повтор уместен."""
    dm = make_manager()
    install_callback_mocks(monkeypatch, dm, payload={}, status=429)

    update = FakeCallbackUpdate('info:447301', from_user=FakeUser(777))
    _run(telegram_bot.handle_movie_detail(update, None))

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_NETWORK
    assert 'retry:info:447301' in _callbacks(kwargs['reply_markup'])


def test_info_callback_permanent_status_has_exits_without_retry(monkeypatch, caplog):
    """404 — постоянный отказ: другой текст и выходы БЕЗ «🔄 Повторить»."""
    dm = make_manager()
    install_callback_mocks(monkeypatch, dm, payload={}, status=404)

    update = FakeCallbackUpdate('info:447301', from_user=FakeUser(777))
    with caplog.at_level(logging.WARNING):
        _run(telegram_bot.handle_movie_detail(update, None))

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_MOVIE_UNAVAILABLE
    assert kwargs.get('parse_mode') == 'HTML'
    callbacks = _callbacks(kwargs['reply_markup'])
    # B7 сохранено: ≥1 действие есть, но повтор заведомо безуспешного запроса
    # не предлагается
    assert len(callbacks) >= 1
    assert not any(c.startswith('retry:info') for c in callbacks)
    assert 'retry:top' in callbacks
    assert telegram_bot.RANDOM_MOVIE_CALLBACK in callbacks
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any('HTTP 404' in m for m in warnings)


# --- 6. Маршрут retry: ---


def test_retry_top_reruns_dialogue_query(monkeypatch):
    """retry:top — запрос «топ комедий» через существующий пайплайн."""
    dm = make_manager()
    dm.process_message = AsyncMock(return_value=make_list_result())
    install_callback_mocks(monkeypatch, dm)

    message = editable_message(photo=())
    update = FakeCallbackUpdate('retry:top', message=message, from_user=FakeUser(777))
    _run(telegram_bot.handle_movie_detail(update, SimpleNamespace(user_data={})))

    assert dm.process_message.await_args.args[2] == telegram_bot._RETRY_TOP_QUERY
    message.edit_text.assert_awaited_once()  # доставка редактированием (A5)


def test_retry_last_uses_saved_query(monkeypatch):
    """retry:last — повтор последнего текстового запроса из user_data."""
    dm = make_manager()
    dm.process_message = AsyncMock(return_value=make_list_result())
    install_callback_mocks(monkeypatch, dm)

    message = editable_message(photo=())
    update = FakeCallbackUpdate('retry:last', message=message, from_user=FakeUser(777))
    context = SimpleNamespace(user_data={telegram_bot._LAST_QUERY_KEY: 'комедии 2020-х'})
    _run(telegram_bot.handle_movie_detail(update, context))

    assert dm.process_message.await_args.args[2] == 'комедии 2020-х'
    message.edit_text.assert_awaited_once()


def test_retry_last_without_context_is_friendly():
    """Потеря контекста (рестарт/воркер) — объяснение + альтернативные действия."""
    update = FakeCallbackUpdate('retry:last', from_user=FakeUser(777))

    _run(telegram_bot.handle_movie_detail(update, None))

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_LOST_RETRY
    callbacks = _callbacks(kwargs['reply_markup'])
    assert 'retry:top' in callbacks
    assert telegram_bot.RANDOM_MOVIE_CALLBACK in callbacks


def test_retry_random_delegates_to_random_route(monkeypatch):
    """retry:random — повторный случайный фильм (карточка редактированием)."""
    dm = make_manager()
    dm.get_random_movie = AsyncMock(return_value=make_card_result())  # type: ignore[method-assign]
    install_callback_mocks(monkeypatch, dm)

    message = editable_message(photo=())
    update = FakeCallbackUpdate('retry:random', message=message, from_user=FakeUser(777))
    _run(telegram_bot.handle_movie_detail(update, None))

    message.edit_media.assert_awaited_once()


def test_retry_info_delegates_to_card_route(monkeypatch):
    """retry:info:{id} — повтор загрузки той же карточки."""
    dm = make_manager()
    install_callback_mocks(monkeypatch, dm)  # KP_PAYLOAD, статус 200

    message = editable_message(photo=())
    update = FakeCallbackUpdate('retry:info:447301', message=message, from_user=FakeUser(777))
    _run(telegram_bot.handle_movie_detail(update, None))

    message.edit_media.assert_awaited_once()
    caption = message.edit_media.await_args.kwargs['media'].caption
    assert 'Начало' in caption


def test_retry_unknown_target_is_generic_with_exit():
    """Неизвестная цель повтора — резервный класс с кнопками выхода."""
    update = FakeCallbackUpdate('retry:whatever', from_user=FakeUser(777))

    _run(telegram_bot.handle_movie_detail(update, None))

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_GENERIC
    callbacks = _callbacks(kwargs['reply_markup'])
    assert 'retry:top' in callbacks
    assert telegram_bot.RANDOM_MOVIE_CALLBACK in callbacks


# --- 6.1 Делегирование остальных целей retry: (nit n6) ---


def test_retry_similar_delegates_to_similar_route(monkeypatch):
    """retry:similar:{id} — повтор подбора похожих тем же маршрутом."""
    dm = make_manager()
    dm.find_similar_by_id = AsyncMock(return_value=make_list_result())
    install_callback_mocks(monkeypatch, dm)

    message = editable_message(photo=())
    update = FakeCallbackUpdate('retry:similar:435', message=message, from_user=FakeUser(777))
    _run(telegram_bot.handle_movie_detail(update, None))

    assert dm.find_similar_by_id.await_args.args[1:] == ('777', 435)
    message.edit_text.assert_awaited_once()


def test_retry_alt_delegates_to_alternative_route(monkeypatch):
    """retry:alt — повтор «других вариантов» тем же запросом."""
    dm = make_manager()
    dm.process_message = AsyncMock(return_value=make_list_result())
    install_callback_mocks(monkeypatch, dm)

    message = editable_message(photo=())
    update = FakeCallbackUpdate('retry:alt', message=message, from_user=FakeUser(777))
    _run(telegram_bot.handle_movie_detail(update, None))

    assert dm.process_message.await_args.args[2] == telegram_bot.OTHER_VARIANTS_QUERY
    message.edit_text.assert_awaited_once()


def test_retry_back_delegates_to_back_route(monkeypatch):
    """retry:back — повтор возврата к списку из сессии."""
    dm = make_manager()
    dm.session_manager.get_session('777').last_movies = [make_movie(id=1, title='Дюна')]
    install_callback_mocks(monkeypatch, dm)

    message = editable_message(photo=())
    update = FakeCallbackUpdate('retry:back', message=message, from_user=FakeUser(777))
    _run(telegram_bot.handle_movie_detail(update, None))

    message.edit_text.assert_awaited_once()
    assert 'Дюна' in message.edit_text.await_args.args[0]


def test_retry_mood_delegates_to_mood_route():
    """retry:mood (minor m4) — повтор приглашения «по настроению»."""
    update = FakeCallbackUpdate('retry:mood', from_user=FakeUser(777))

    _run(telegram_bot.handle_movie_detail(update, None))

    text, kwargs = update.callback_query.message.texts[0]
    assert text == telegram_bot._MOOD_PROMPT_HTML
    assert kwargs.get('parse_mode') == 'HTML'
    assert update.callback_query.answer_calls == 1


class _FailOnceMessage(FakeMessage):
    """Сообщение, у которого ПЕРВЫЙ reply_text падает (сбой доставки).

    Второй вызов (доставка самой ошибки через `_callback_failure`) проходит —
    так проверяется ветка исключения маршрута, а не обработчик отказов.
    """

    def __init__(self, chat: Any = None):
        super().__init__(chat=chat)
        self.failures = 0

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        if self.failures == 0:
            self.failures += 1
            raise NetworkError('Telegram недоступен')
        await super().reply_text(text, **kwargs)


def test_mood_callback_failure_retries_same_operation(caplog):
    """minor m4: сбой маршрута mood: — «🔄 Повторить» ведёт на retry:mood."""
    message = _FailOnceMessage(chat=FakeChat())
    update = FakeCallbackUpdate(telegram_bot.MOOD_START_CALLBACK, message=message, from_user=FakeUser(777))

    with caplog.at_level(logging.ERROR):
        _run(telegram_bot.handle_movie_detail(update, None))

    text, kwargs = message.texts[0]
    assert text == telegram_bot._ERROR_TEXT_NETWORK
    callbacks = _callbacks(kwargs['reply_markup'])
    assert 'retry:mood' in callbacks
    # Повтор предлагает ТУ ЖЕ операцию, а не случайный фильм (design.md D3)
    assert 'retry:random' not in callbacks
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)


# --- 6.2 Служебный ответ без inline-кнопок (nit n6) ---


def test_unknown_user_service_reply_keeps_menu_as_action():
    """Ветка `_callback_failure` без клавиатуры: reply-меню — это действие.

    «Не удалось определить пользователя» — НЕ ошибка класса, а служебный
    ответ: следующий шаг — ввод текстом, а два reply_markup в одном
    сообщении Telegram не поддерживает (design.md D3). Фиксируем, что меню
    прикреплено и текст отправлен в HTML.
    """
    update = FakeCallbackUpdate('retry:top', from_user=None)

    _run(telegram_bot.handle_movie_detail(update, None))

    text, kwargs = update.callback_query.message.texts[0]
    assert 'Не удалось определить пользователя' in text
    assert kwargs.get('parse_mode') == 'HTML'
    assert isinstance(kwargs.get('reply_markup'), ReplyKeyboardMarkup)


# --- 7. Длина пользовательского текста в логах (nit n8) ---


def test_log_excerpt_truncates_user_text():
    """Обрезка для лога: не более `_LOG_QUERY_LIMIT` символов."""
    assert telegram_bot._log_excerpt('а' * 500) == 'а' * telegram_bot._LOG_QUERY_LIMIT
    assert telegram_bot._log_excerpt('короткий') == 'короткий'


def test_process_and_reply_logs_truncated_query(monkeypatch, caplog):
    """Длинный запрос попадает в лог обрезанным (поведение ответа не меняется)."""
    dm = make_manager()
    dm.process_message = AsyncMock(side_effect=aiohttp.ClientError('нет связи'))
    install_callback_mocks(monkeypatch, dm)

    long_query = 'комедия ' * 60
    update = _process_update()
    with caplog.at_level(logging.ERROR):
        _run(telegram_bot._process_and_reply(update, SimpleNamespace(user_data={}), '777', long_query))

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert messages
    assert all(long_query not in m for m in messages)
    assert all(long_query[:telegram_bot._LOG_QUERY_LIMIT] in m for m in messages)
    # Ответ пользователю — как раньше: классифицированная ошибка с кнопками
    assert update.message.texts[0][0] == telegram_bot._ERROR_TEXT_NETWORK
