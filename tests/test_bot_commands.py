"""Тесты регистрации команд бота (A1) и единого HTML-форматирования (A6).

Изменение: add-telegram-commands-and-html-parsemode; дополнено правками
add-onboarding-and-error-recovery (minor m1 — описание /start соответствует
онбордингу, minor m2 — справка возвращает reply-меню как якорь навигации).
Все проверки офлайн — используются фейковые application/bot/update без сети
и реального Telegram.
"""
import asyncio
import logging
from typing import Any, List, Tuple

from telegram import BotCommand, ReplyKeyboardMarkup

import telegram_bot


# --- A1: состав и ограничения команд -------------------------------------


def test_build_bot_commands_names_and_limits():
    """build_bot_commands возвращает 7 команд с ожидаемыми именами и описаниями."""
    commands = telegram_bot.build_bot_commands()

    assert isinstance(commands, list)
    assert [c.command for c in commands] == [
        "start", "help", "movie", "top", "genre", "mood", "list",
    ]
    for cmd in commands:
        assert isinstance(cmd, BotCommand)
        assert cmd.description  # непустое описание
        assert len(cmd.description) <= 256  # лимит меню Telegram


def test_start_command_description_matches_onboarding():
    """minor m1: описание /start соответствует фактическому поведению (B1).

    После онбординга команда НЕ открывает reply-меню, а сразу предлагает
    ценность в одно нажатие — описание в меню команд клиента не должно
    обещать главное меню.
    """
    descriptions = {c.command: c.description for c in telegram_bot.build_bot_commands()}

    start_description = descriptions['start']
    assert 'меню' not in start_description.lower()
    assert 'случайный фильм' in start_description.lower()
    assert 'настроени' in start_description.lower()
    assert len(start_description) <= 256


# --- A1: hook вызывает setMyCommands --------------------------------------


class _FakeBot:
    """Фейковый бот: записывает вызовы set_my_commands."""

    def __init__(self, raise_exc: Exception | None = None):
        self.calls: List[Any] = []
        self._raise = raise_exc

    async def set_my_commands(self, commands: Any) -> bool:
        if self._raise is not None:
            raise self._raise
        self.calls.append(commands)
        return True


class _FakeApplication:
    def __init__(self, bot: _FakeBot):
        self.bot = bot


def test_register_bot_commands_calls_set_my_commands():
    """register_bot_commands вызывает bot.set_my_commands со списком команд."""
    bot = _FakeBot()
    app = _FakeApplication(bot)

    asyncio.run(telegram_bot.register_bot_commands(app))  # type: ignore[arg-type]

    assert len(bot.calls) == 1
    assert bot.calls[0] == telegram_bot.build_bot_commands()


def test_register_bot_commands_is_fail_silent(caplog):
    """Сбой setMyCommands логируется как warning и не роняет hook."""
    bot = _FakeBot(raise_exc=RuntimeError("Telegram недоступен"))
    app = _FakeApplication(bot)

    with caplog.at_level(logging.WARNING):
        # Не должно выбросить исключение наружу
        asyncio.run(telegram_bot.register_bot_commands(app))  # type: ignore[arg-type]

    assert bot.calls == []
    assert any(
        rec.levelno == logging.WARNING
        and "команд" in rec.getMessage().lower()
        for rec in caplog.records
    )


# --- A6: текст настроения в HTML ------------------------------------------


class _FakeMessage:
    def __init__(self):
        self.texts: List[Tuple[str, Any]] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.texts.append((text, kwargs))


class _FakeUpdate:
    def __init__(self):
        self.message = _FakeMessage()


def test_handle_mood_command_sends_html():
    """Команда /mood отправляет HTML-текст с <i>, без '*', parse_mode='HTML'."""
    update = _FakeUpdate()

    asyncio.run(telegram_bot.handle_mood_command(update, None))

    assert len(update.message.texts) == 1
    text, kwargs = update.message.texts[0]
    assert kwargs.get("parse_mode") == "HTML"
    assert "*" not in text
    assert "<i>" in text and "</i>" in text
    # Единый источник текста (DRY)
    assert text == telegram_bot._MOOD_PROMPT_HTML


# --- B1 (minor m2): справка возвращает якорь reply-меню ---


def test_handle_help_attaches_main_menu_anchor():
    """/help снова показывает reply-меню: постоянный якорь навигации до B8.

    /start после B1 меню не крепит (одно сообщение не несёт два
    reply_markup), поэтому справка — точка, где пользователь гарантированно
    видит меню и следующий шаг. Требование «ровно одно сообщение» у /start
    при этом не нарушается.
    """
    update = _FakeUpdate()

    asyncio.run(telegram_bot.handle_help(update, None))

    assert len(update.message.texts) == 1
    _, kwargs = update.message.texts[0]
    assert kwargs.get("parse_mode") == "HTML"
    markup = kwargs.get("reply_markup")
    assert isinstance(markup, ReplyKeyboardMarkup)
    menu_texts = {b.text for row in markup.keyboard for b in row}
    assert "🎭 Фильм по настроению" in menu_texts
    assert "💡 Помощь" in menu_texts
