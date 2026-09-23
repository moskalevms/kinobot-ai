"""Тесты A5: доставка callback-результата редактированием сообщения.

Без сети: проверяется матрица переходов `_edit_or_send` в редакции ревью
(design.md D2): текст→текст — `edit_text`; ЛЮБОЕ→фото — `edit_media`
(PTB 22.5 / Bot API 9.2 умеет добавлять медиа к текстовому сообщению —
идемпотентно, без удаления и дублей); медиа→текст — детерминированные
«новое сообщение (без цитаты) → удаление исходного» (editMessageText к
медиа неприменим); отказ редактирования — fallback «сначала отправка,
затем удаление»; «message is not modified» — молча. Плюс gate
`isinstance(message, Message)` (фейки и InaccessibleMessage) и
edit-семантика всех четырёх маршрутов через `handle_movie_detail`.
Мок-паттерны — общие фейки из `tests/conftest.py` (nit n4, изменение
verify-phase0-uiux-tests); редактируемое
сообщение — `AsyncMock(spec=Message)` (проходит gate, edit-методы awaitable).
"""
import asyncio
import logging
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock, MagicMock

import telegram_bot
from conftest import (
    POSTER_URL,
    FakeCallbackUpdate,
    FakeHttpSession,
    FakeMessage,
    FakeUser,
    editable_message as _editable_message,
    install_callback_mocks as _install_callback_mocks,
    make_card_result as _card_result,
    make_list_result as _list_result,
    make_manager as _manager,
    make_movie as _movie,
    recording_mock as _recording,
)
from conftest import KP_PAYLOAD as _KP_PAYLOAD
from dialogue_manager import CARD_BACK_CALLBACK
from telegram import Chat, InaccessibleMessage, Message
from telegram.error import BadRequest

NOT_MODIFIED_ERROR = (
    'Message is not modified: specified new message content and reply markup '
    'are exactly the same as a current content of the message'
)
CANT_EDIT_ERROR = "Bad Request: message can't be edited"
CANT_DELETE_ERROR = "Bad Request: message can't be deleted"

# Фото-сообщение: достаточно непустого кортежа photo (форма, не содержимое)
_PHOTO = (SimpleNamespace(file_unique_id='x'),)


def _run(coro):
    return asyncio.run(coro)


def _warnings(caplog) -> List[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING and r.name == 'telegram']


# --- 6.1 Матрица переходов _edit_or_send (design.md D2, редакция ревью) ---


def test_text_to_text_uses_edit_text_only():
    """Переход №1: список → список (alt:/back:) — только edit_text."""
    message = _editable_message(photo=())
    result = _list_result()

    _run(telegram_bot._edit_or_send(message, result))

    message.edit_text.assert_awaited_once_with(
        result['response'], parse_mode='HTML', reply_markup=result['reply_markup']
    )
    message.edit_media.assert_not_awaited()
    message.delete.assert_not_awaited()
    message.reply_text.assert_not_awaited()
    message.reply_photo.assert_not_awaited()


def test_text_to_photo_uses_edit_media_in_place():
    """Переход №2 (M1): текстовый список → фото-карточка ОДНИМ edit_media.

    Bot API 9.2 (PTB 22.5) документирует editMessageMedia в том числе как
    «add media to text messages»: ни удаления, ни нового сообщения —
    критерий приёмки A5 «в чате не появляется новых сообщений» выполнен,
    а доставка идемпотентна (двойной тап — «not modified», см. ниже).
    """
    message = _editable_message(photo=())
    result = _card_result()

    _run(telegram_bot._edit_or_send(message, result))

    message.edit_media.assert_awaited_once()
    kwargs = message.edit_media.await_args.kwargs
    media = kwargs['media']
    assert media.media == POSTER_URL
    assert media.caption == result['response']
    assert media.parse_mode == 'HTML'
    assert kwargs['reply_markup'] is result['reply_markup']
    message.edit_text.assert_not_awaited()
    message.delete.assert_not_awaited()
    message.reply_photo.assert_not_awaited()
    message.reply_text.assert_not_awaited()


def test_photo_to_photo_uses_edit_media_with_input_media_photo():
    """Переход №2 (фото→фото): карточка → карточка другого фильма."""
    message = _editable_message(photo=_PHOTO)
    result = _card_result()

    _run(telegram_bot._edit_or_send(message, result))

    message.edit_media.assert_awaited_once()
    kwargs = message.edit_media.await_args.kwargs
    media = kwargs['media']
    assert media.media == POSTER_URL
    assert media.caption == result['response']
    assert media.parse_mode == 'HTML'
    assert kwargs['reply_markup'] is result['reply_markup']
    message.delete.assert_not_awaited()
    message.reply_photo.assert_not_awaited()
    message.reply_text.assert_not_awaited()


def test_media_to_text_sends_new_message_then_deletes():
    """Переход №3 (M2): медиа→текст нередактируемо — сначала send, затем delete.

    `editMessageText` применим только к текстовым/игровым сообщениям:
    обречённый вызов и warning на штатном пути исключены. Порядок
    «send → delete»: при сбое отправки исходная карточка цела; новое
    сообщение не цитирует заменяемое (`do_quote=False` — иначе в
    непубличном чате PTB подставил бы ReplyParameters на удалённое
    сообщение и Telegram ответил бы «replied message not found»).
    """
    message = _editable_message(photo=_PHOTO)
    calls: List[str] = []
    message.reply_text = _recording(calls, 'send')
    message.delete = _recording(calls, 'delete')
    result = _list_result()

    _run(telegram_bot._edit_or_send(message, result))

    message.edit_text.assert_not_awaited()
    assert calls == ['send', 'delete']
    assert message.reply_text.await_args.args[0] == result['response']
    kwargs = message.reply_text.await_args.kwargs
    assert kwargs['parse_mode'] == 'HTML'
    assert kwargs['reply_markup'] is result['reply_markup']
    assert kwargs['do_quote'] is False


def test_double_tap_list_to_card_is_idempotent(caplog):
    """Двойной тап «список → карточка»: «not modified» — молча, без дубля."""
    message = _editable_message(photo=())
    message.edit_media.side_effect = BadRequest(NOT_MODIFIED_ERROR)

    with caplog.at_level(logging.DEBUG):
        _run(telegram_bot._edit_or_send(message, _card_result()))

    assert not _warnings(caplog)
    message.delete.assert_not_awaited()
    message.reply_photo.assert_not_awaited()
    message.reply_text.assert_not_awaited()


def test_delete_failure_after_send_keeps_delivery(caplog):
    """Отказ удаления (>48 ч/нет прав): контент УЖЕ доставлен, warning в лог."""
    message = _editable_message(photo=_PHOTO)
    message.delete.side_effect = BadRequest(CANT_DELETE_ERROR)

    with caplog.at_level(logging.WARNING):
        _run(telegram_bot._edit_or_send(message, _list_result()))

    message.reply_text.assert_awaited_once()
    assert _warnings(caplog)


# --- 6.2 Отказы сервера и gate редактируемости ---


def test_edit_text_refused_falls_back_send_then_delete(caplog):
    """Отказ edit_text (текст→текст): warning, СНАЧАЛА send, ЗАТЕМ delete."""
    message = _editable_message(photo=())
    message.edit_text.side_effect = BadRequest(CANT_EDIT_ERROR)
    calls: List[str] = []
    message.reply_text = _recording(calls, 'send')
    message.delete = _recording(calls, 'delete')

    with caplog.at_level(logging.WARNING):
        _run(telegram_bot._edit_or_send(message, _list_result()))

    assert _warnings(caplog)
    assert calls == ['send', 'delete']
    assert message.reply_text.await_args.kwargs['do_quote'] is False


def test_edit_media_refused_falls_back_send_then_delete(caplog):
    """Отказ edit_media: тот же fallback — сначала send, затем delete."""
    message = _editable_message(photo=())
    message.edit_media.side_effect = BadRequest(CANT_EDIT_ERROR)
    calls: List[str] = []
    message.reply_photo = _recording(calls, 'send')
    message.delete = _recording(calls, 'delete')

    with caplog.at_level(logging.WARNING):
        _run(telegram_bot._edit_or_send(message, _card_result()))

    assert _warnings(caplog)
    assert calls == ['send', 'delete']
    assert message.reply_photo.await_args.kwargs['do_quote'] is False


def test_not_modified_is_ignored_silently_on_edit_text(caplog):
    """Повторный тап (текст→текст): «not modified» — без warning и доставки."""
    message = _editable_message(photo=())
    message.edit_text.side_effect = BadRequest(NOT_MODIFIED_ERROR)

    with caplog.at_level(logging.DEBUG):
        _run(telegram_bot._edit_or_send(message, _list_result()))

    assert not _warnings(caplog)
    message.delete.assert_not_awaited()
    message.reply_text.assert_not_awaited()
    message.reply_photo.assert_not_awaited()


def test_not_modified_is_ignored_silently_on_edit_media(caplog):
    message = _editable_message(photo=_PHOTO)
    message.edit_media.side_effect = BadRequest(NOT_MODIFIED_ERROR)

    with caplog.at_level(logging.DEBUG):
        _run(telegram_bot._edit_or_send(message, _card_result()))

    assert not _warnings(caplog)
    message.delete.assert_not_awaited()
    message.reply_photo.assert_not_awaited()


def test_non_bad_request_propagates_to_route_handler():
    """Сетевые сбои не дублируют контур A3/A4 — всплывают в except маршрута."""
    message = _editable_message(photo=())
    message.edit_text.side_effect = RuntimeError('сеть недоступна')

    try:
        _run(telegram_bot._edit_or_send(message, _list_result()))
        assert False, 'ожидалось исключение'
    except RuntimeError:
        pass
    message.reply_text.assert_not_awaited()


def test_non_message_fake_falls_back_to_send_result():
    """Gate: фейк без edit-методов (не PTB Message) — прежняя доставка."""
    fake = FakeMessage()

    _run(telegram_bot._edit_or_send(fake, _card_result()))

    assert len(fake.photos) == 1
    assert fake.photos[0][0] == POSTER_URL
    assert fake.texts == []


def test_inaccessible_message_falls_back_to_send_result():
    """Gate (n5): InaccessibleMessage не Message — доставка `_send_result`.

    У реального `InaccessibleMessage` нет edit-методов (и reply-* тоже) —
    gate направляет доставку в `_send_result`, а AttributeError на
    `reply_photo` обрабатывает контур отказоустойчивости маршрута (A3/A4).
    Здесь reply-методы добавлены, чтобы проверить именно ветвление gate.
    """
    # Sanity: реальный InaccessibleMessage не проходит isinstance-gate
    real = InaccessibleMessage(chat=Chat(id=1, type='private'), message_id=0)
    assert not isinstance(real, Message)

    message = MagicMock(spec=InaccessibleMessage)
    message.reply_photo = AsyncMock()
    message.reply_text = AsyncMock()

    _run(telegram_bot._edit_or_send(message, _card_result()))

    message.reply_photo.assert_awaited_once()
    message.reply_text.assert_not_awaited()


# --- 6.3 Хелперы ---


def test_is_not_modified_error_detection():
    """Маркер 'not modified' (n2): короче и меньше зависит от формулировки."""
    assert telegram_bot._is_not_modified_error(BadRequest(NOT_MODIFIED_ERROR))
    assert telegram_bot._is_not_modified_error(BadRequest('message is not modified'))
    assert telegram_bot._is_not_modified_error(BadRequest('Not Modified'))
    assert not telegram_bot._is_not_modified_error(BadRequest(CANT_EDIT_ERROR))
    assert not telegram_bot._is_not_modified_error(BadRequest(CANT_DELETE_ERROR))
    assert not telegram_bot._is_not_modified_error(BadRequest(''))


def test_delete_message_quietly_swallows_errors(caplog):
    message = _editable_message()
    message.delete.side_effect = BadRequest(CANT_DELETE_ERROR)

    with caplog.at_level(logging.WARNING):
        _run(telegram_bot._delete_message_quietly(message))

    assert _warnings(caplog)


def test_delete_message_quietly_without_delete_method():
    """У объекта нет delete — молча пропускаем (фейки/InaccessibleMessage)."""
    _run(telegram_bot._delete_message_quietly(object()))


# --- 6.4 Маршруты через handle_movie_detail ---


def test_info_callback_from_text_list_replaces_message(monkeypatch):
    """info: из текстового списка с постером: ОДИН edit_media, без delete/reply."""
    dm = _manager()
    _install_callback_mocks(monkeypatch, dm)
    message = _editable_message(photo=())
    update = FakeCallbackUpdate('info:447301', message=message, from_user=FakeUser(777))

    _run(telegram_bot.handle_movie_detail(update, None))

    message.edit_media.assert_awaited_once()
    kwargs = message.edit_media.await_args.kwargs
    assert kwargs['media'].media == POSTER_URL
    caption = kwargs['media'].caption
    assert caption.startswith('🎬 <strong>Начало (2010)')
    assert '🍅 91%' in caption
    assert '<blockquote expandable>' in caption
    assert kwargs['reply_markup'] is not None
    message.edit_text.assert_not_awaited()
    message.delete.assert_not_awaited()
    message.reply_text.assert_not_awaited()
    message.reply_photo.assert_not_awaited()
    assert update.callback_query.answer_calls == 1


def test_info_callback_without_poster_edits_list_message(monkeypatch):
    """info: без постера: текстовый список редактируется в текстовую карточку."""
    dm = _manager()
    payload = {**_KP_PAYLOAD, 'poster': {'url': ''}}
    _install_callback_mocks(monkeypatch, dm, payload=payload)
    message = _editable_message(photo=())
    update = FakeCallbackUpdate('info:447301', message=message, from_user=FakeUser(777))

    _run(telegram_bot.handle_movie_detail(update, None))

    message.edit_text.assert_awaited_once()
    text = message.edit_text.await_args.args[0]
    assert text.startswith('🎬 <strong>Начало (2010)')
    assert message.edit_text.await_args.kwargs['parse_mode'] == 'HTML'
    assert message.edit_text.await_args.kwargs['reply_markup'] is not None
    message.delete.assert_not_awaited()
    message.reply_text.assert_not_awaited()
    message.reply_photo.assert_not_awaited()
    assert update.callback_query.answer_calls == 1


def test_back_callback_from_photo_card_replaces_message_with_list(monkeypatch):
    """back: из фото-карточки (M2): список — новым сообщением, карточка удаляется."""
    dm = _manager()
    movies = [_movie(id=i, title=f'Фильм {i}') for i in range(1, 4)]
    dm.session_manager.get_session('777').last_movies = list(movies)
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    message = _editable_message(photo=_PHOTO)
    calls: List[str] = []
    message.reply_text = _recording(calls, 'send')
    message.delete = _recording(calls, 'delete')
    update = FakeCallbackUpdate(CARD_BACK_CALLBACK, message=message, from_user=FakeUser(777))

    _run(telegram_bot.handle_movie_detail(update, None))

    expected_text, expected_markup = dm.render_movie_list(movies, telegram_bot.BACK_TO_LIST_HEADER)
    message.edit_text.assert_not_awaited()
    assert calls == ['send', 'delete']  # нетто одно сообщение, история чистая
    assert message.reply_text.await_args.args[0] == expected_text
    kwargs = message.reply_text.await_args.kwargs
    assert kwargs['parse_mode'] == 'HTML'
    assert kwargs['reply_markup'].to_dict() == expected_markup.to_dict()
    assert kwargs['do_quote'] is False
    message.reply_photo.assert_not_awaited()
    assert update.callback_query.answer_calls == 1


def test_back_callback_from_text_card_edits_in_place(monkeypatch):
    """back: из текстовой карточки (без постера): обратное редактирование на месте."""
    dm = _manager()
    movies = [_movie(id=i, title=f'Фильм {i}') for i in range(1, 4)]
    dm.session_manager.get_session('777').last_movies = list(movies)
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    message = _editable_message(photo=())
    update = FakeCallbackUpdate(CARD_BACK_CALLBACK, message=message, from_user=FakeUser(777))

    _run(telegram_bot.handle_movie_detail(update, None))

    expected_text, expected_markup = dm.render_movie_list(movies, telegram_bot.BACK_TO_LIST_HEADER)
    message.edit_text.assert_awaited_once()
    assert message.edit_text.await_args.args[0] == expected_text
    assert message.edit_text.await_args.kwargs['reply_markup'].to_dict() == expected_markup.to_dict()
    message.reply_text.assert_not_awaited()
    message.reply_photo.assert_not_awaited()
    message.delete.assert_not_awaited()
    assert update.callback_query.answer_calls == 1


def test_back_callback_double_tap_is_silent(monkeypatch, caplog):
    """Двойной тап «⬅️ К списку»: «not modified» молча игнорируется."""
    dm = _manager()
    dm.session_manager.get_session('777').last_movies = [_movie()]
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    message = _editable_message(photo=())
    message.edit_text.side_effect = BadRequest(NOT_MODIFIED_ERROR)
    update = FakeCallbackUpdate(CARD_BACK_CALLBACK, message=message, from_user=FakeUser(777))

    with caplog.at_level(logging.DEBUG):
        _run(telegram_bot.handle_movie_detail(update, None))

    assert not _warnings(caplog)
    message.reply_text.assert_not_awaited()
    message.reply_photo.assert_not_awaited()
    message.delete.assert_not_awaited()
    assert update.callback_query.answer_calls == 1


def test_similar_callback_from_photo_card_sends_list_then_deletes(monkeypatch):
    """similar: из фото-карточки (M2): список — send, затем delete карточки."""
    dm = _manager()
    session = dm.session_manager.get_session('777')
    session.last_movies = [_movie(id=435, title='Дюна'), _movie(id=436, title='Другой')]
    dm.movie_agent.recommend_movies = AsyncMock(return_value=[_movie(id=900, title='Похожий фильм')])
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    monkeypatch.setattr(telegram_bot.aiohttp, 'ClientSession', lambda *a, **kw: FakeHttpSession())
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda *a, **kw: None)
    message = _editable_message(photo=_PHOTO)
    calls: List[str] = []
    message.reply_text = _recording(calls, 'send')
    message.delete = _recording(calls, 'delete')
    update = FakeCallbackUpdate('similar:435', message=message, from_user=FakeUser(777))

    _run(telegram_bot.handle_movie_detail(update, None))

    message.edit_text.assert_not_awaited()
    assert calls == ['send', 'delete']
    text = message.reply_text.await_args.args[0]
    assert 'Похожий фильм' in text
    assert message.reply_text.await_args.kwargs['parse_mode'] == 'HTML'
    assert update.callback_query.answer_calls == 1


def test_alt_callback_edits_list_in_place(monkeypatch):
    """alt: под текстовым списком: новый список редактирует прежний."""
    dm = _manager()
    dm.process_message = AsyncMock(return_value={
        'response': '<strong>Вот другие варианты:</strong>\n',
        'reply_markup': None,
    })
    monkeypatch.setattr(telegram_bot, 'dialogue_manager', dm)
    monkeypatch.setattr(telegram_bot.aiohttp, 'ClientSession', lambda *a, **kw: FakeHttpSession())
    monkeypatch.setattr(telegram_bot, 'track_client_request', lambda *a, **kw: None)
    message = _editable_message(photo=())
    update = FakeCallbackUpdate('alt:list', message=message, from_user=FakeUser(777))

    _run(telegram_bot.handle_movie_detail(update, None))

    message.edit_text.assert_awaited_once_with(
        '<strong>Вот другие варианты:</strong>\n', parse_mode='HTML', reply_markup=None
    )
    message.reply_text.assert_not_awaited()
    message.reply_photo.assert_not_awaited()
    message.delete.assert_not_awaited()
    assert update.callback_query.answer_calls == 1
