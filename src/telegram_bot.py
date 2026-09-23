# src/telegram_bot.py
import asyncio
import os
import logging
from typing import Any, Awaitable, Callable, Dict, Literal, Optional
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message
)
from telegram.error import BadRequest, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler
)
from dotenv import load_dotenv
from session_manager import SessionManager
# format_movie_card переэкспортируется намеренно: общий форматтер карточки
# обязан быть доступен из модуля бота (проверяется тестом обратной
# совместимости RT-бейджа). Карточку собирает build_movie_card поверх него,
# поэтому прямых вызовов format_movie_card здесь больше нет.
from dialogue_manager import (
    DialogueManager,
    PAGE_CALLBACK_PREFIX,
    RANDOM_MOVIE_CALLBACK,
    SAVE_CALLBACK_PREFIX,
    UNSAVE_CALLBACK_PREFIX,
    WATCHLIST_CALLBACK,
    WATCHLIST_OPEN_BUTTON_TEXT,
    WATCHLIST_PAGE_LIMIT,
    WATCHLIST_PAGE_PREFIX,
    build_movie_card,
    clamp_page_offset,
    compute_list_hash,
    format_movie_card,  # noqa: F401
    render_watchlist_page,
    truncate_button_text,
)
from watchlist_manager import get_watchlist_manager
from statistics_tracker import track_client_request
from config import CURRENT_YEAR
from guardrails import sanitize_message
from kinopoisk_client import is_kinopoisk_transient_error
from utils.movie_filter import extract_imdb_id
from rt_enrichment import enrich_movies_with_rt_scores
import aiohttp

load_dotenv()
from log_setup import setup_logging
logger = setup_logging("telegram")

# Инициализация менеджеров (для polling-режима)
session_manager = SessionManager()
dialogue_manager = DialogueManager(session_manager)

# Единый текст приглашения «по настроению» в HTML. Используется и командой
# /mood, и кнопкой «🎭 Фильм по настроению» — единый источник исключает
# рассинхрон форматирования (A6). Курсив передаёт тег <i> (единый HTML).
_MOOD_PROMPT_HTML = (
    "🎭 Какое у вас сейчас настроение?\n"
    "Например: <i>грустное</i>, <i>весёлое</i>, <i>устал</i>, <i>скучно</i>, "
    "<i>хочу адреналина</i>, <i>страшно</i>, <i>романтическое</i>."
)

# Запрос «другие варианты» — общий для reply-кнопки «🔄 Другие варианты» и
# inline-кнопки под списком (A3): один источник исключает рассинхрон
# поведения двух кнопок с одинаковым смыслом.
OTHER_VARIANTS_QUERY = "посоветуй другие фильмы"

# Заголовок списка, восстановленного по кнопке «⬅️ К списку» (A4)
BACK_TO_LIST_HEADER = "Ваш список фильмов:"

# --- Контекстные quick replies и пагинация списка (фаза 3, B2/B3) ---
# Заголовок страницы-продолжения (B3): исходный заголовок выдачи в сессии
# не хранится (схема БД не меняется), контекст дают нейтральный заголовок
# и сквозная нумерация строк/кнопок.
PAGE_CONTINUATION_HEADER = "Продолжение списка:"
# Устаревшая кнопка пагинации (B3, design.md D6): выдача перезаписана новым
# подбором (hash разошёлся) или сессия пуста — объяснение + выходы
# «⬅️ К списку» / «🎲 Случайный фильм» (B7: без dead-end), сообщение
# списка НЕ редактируется.
_ERROR_TEXT_STALE_PAGE = (
    "Подборка обновилась — кнопка устарела.\n"
    "Вернитесь к актуальному списку — или подберу случайный фильм сразу."
)
# Ключ снапшота текущей страницы выдачи в context.user_data (B3, design.md
# D5): {'offset': int, 'hash': str}. In-memory: потеря при рестарте/между
# воркерами допустима — деградация до возврата на первую страницу, не
# ошибка (образец — _LAST_QUERY_KEY и снапшот A5).
_LIST_PAGE_KEY = 'list_page'

# --- Онбординг «ценность сразу» (фаза 3, B1, add-onboarding-and-error-recovery) ---
# Приветствие: не более 4 строк (кто я / что делаю / CTA), эмодзи по
# docs/emoji_guideline.md (🎬 — кино/бот, ⚠️ — ограничение), HTML (A6).
_WELCOME_HTML = (
    "🎬 <b>Кинобот</b> — подскажу, что посмотреть вечером.\n"
    "Опишите настроение парой слов или нажмите кнопку ниже — подберу за пару секунд.\n"
    "⚠️ Отвечаю только про кино и сериалы."
)
# Кнопки онбординга: мгновенная ценность без ввода текста. Подписи и
# callback_data — константы (единый источник для клавиатуры и тестов);
# 🎲 закреплён гайдлайном за «случайный фильм», 🎭 — за подбор по настроению.
# RANDOM_MOVIE_CALLBACK импортируется из dialogue_manager (B2): тем же
# литералом собирается кнопка «🎲 Случайный» ряда навигации списка.
RANDOM_MOVIE_BUTTON_TEXT = '🎲 Случайный фильм вечера'
MOOD_BUTTON_TEXT = '🎭 Подобрать по настроению'
MOOD_START_CALLBACK = 'mood:start'

# --- Дружелюбные ошибки с выходом (фаза 3, B7, add-onboarding-and-error-recovery) ---
# Классы ошибок: каждый получает свой текст и свои кнопки выхода —
# безликий генерик без действий запрещён (дельта-спека error-recovery).
# Тексты — константы: единый источник для всех путей.
# Набор классов ЗАКРЫТЫЙ (Literal): mypy ловит опечатку в имени класса на
# этапе проверки, а не в рантайме у пользователя.
ErrorClass = Literal['network', 'llm', 'generic']

ERROR_CLASS_NETWORK: ErrorClass = 'network'   # сеть / Kinopoisk API
ERROR_CLASS_LLM: ErrorClass = 'llm'           # LLM / классификация запроса
ERROR_CLASS_GENERIC: ErrorClass = 'generic'   # прочее (резервный класс, тоже с кнопками)

_ERROR_TEXT_NETWORK = "⚠️ Кинопоиск не отвечает, пробуем ещё раз."
_ERROR_TEXT_LLM = "⚠️ Не получилось понять запрос."
_ERROR_TEXT_GENERIC = "⚠️ Что-то пошло не так, но я не сдаюсь."
_ERROR_TEXT_UNKNOWN_CALLBACK = (
    "Кнопка устарела или неизвестна.\n"
    "Вернитесь к списку — или подберу случайный фильм сразу."
)
_ERROR_TEXT_LOST_RETRY = (
    "Последний запрос не сохранился — возможно, бот перезапускался.\n"
    "Попробуйте мгновенное действие:"
)
# Постоянный отказ Kinopoisk (прочие 4xx: 401/403/404 …) — повтор заведомо
# безуспешен, поэтому текст НЕ предлагает «🔄 Повторить», а уводит на другие
# действия (требование B7 «≥1 действие» выполняется кнопками выхода).
_ERROR_TEXT_MOVIE_UNAVAILABLE = (
    "⚠️ Не удалось загрузить информацию о фильме.\n"
    "Выберите другое действие — или напишите, что хотите посмотреть."
)
# Статусы Kinopoisk, при которых повтор имеет смысл: лимит запросов и сбои
# на стороне сервиса (design.md D2, minor m3). Прочие 4xx — постоянные.
_KINOPOISK_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
# Ограничение длины пользовательского текста в логе (nit n8): запрос может
# быть длинным, а диагностике достаточно начала. Поведение не меняется —
# сам текст обрабатывается как раньше (guardrails.sanitize_message).
_LOG_QUERY_LIMIT = 80
# Подписи кнопок выхода (эмодзи по гайдлайну: 🔄 — повтор, 🎲 — случайный
# фильм, 🏆 — топ, ⬅️ — назад/к списку).
_RETRY_BUTTON_TEXT = '🔄 Повторить'
_EXIT_RANDOM_BUTTON_TEXT = '🎲 Случайный фильм'
_EXIT_TOP_BUTTON_TEXT = '🏆 Топ комедий'
_EXIT_BACK_BUTTON_TEXT = '⬅️ К списку'
# Префикс callback-маршрута повтора (B7) и его цели: retry:last — последний
# текстовый запрос, retry:random — случайный фильм, retry:mood — приглашение
# «по настроению», retry:top — «топ комедий»,
# retry:info:{id}/retry:similar:{id}/retry:alt/retry:back — повтор операции
# соответствующего callback-маршрута («🔄 Повторить» повторяет ТУ ЖЕ
# операцию — design.md D3).
_RETRY_PREFIX = 'retry:'
_RETRY_TOP_QUERY = 'топ комедий'
# Ключ последнего текстового запроса в context.user_data (in-memory:
# осознанное ограничение AGENTS.md — при потере контекста кнопка повтора
# даёт дружелюбный выход с альтернативными действиями).
_LAST_QUERY_KEY = 'last_query'
# Текстовые маркеры LLM-класса: GigaChat поднимает Exception со словами
# «Ошибка GigaChat API» / «Ошибка получения токена»; DeepSeek — через
# LLMRouter. Маркер «классиф» покрывает сбои разбора запроса.
_LLM_ERROR_MARKERS = ('gigachat', 'deepseek', 'llm', 'токен', 'классиф')

# --- Watchlist «📌 Мой список» (фаза 3, B4, add-watchlist) ---
# Тексты ответа на сохранение: доставляются редактированием сообщения
# карточки (A5), в обоих исходах прикладывается кнопка «📌 Мой список»
# (watchlist:view) — мгновенный переход к списку без dead-end.
_SAVED_TEXT = '✅ Сохранено в «Мой список».'
_ALREADY_SAVED_TEXT = 'Уже в списке.'
# Данные фильма для записи берутся из session.last_movies: подборка
# перезаписана или сессия пуста (рестарт) — сохранить из старой карточки
# нельзя, дружелюбное объяснение с действиями-выходами (B7), без падения.
_ERROR_TEXT_SAVE_NO_MOVIE = (
    "Не удалось сохранить фильм: подборка обновилась или сессия пуста.\n"
    "Откройте карточку фильма ещё раз и нажмите «📌 Сохранить»."
)
# Пункт главного reply-меню (диспетчеризация по точному тексту в
# handle_message — существующий паттерн; B8 переведёт меню на inline).
WATCHLIST_MENU_BUTTON_TEXT = '📌 Мой список'


def _log_excerpt(text: str, limit: int = _LOG_QUERY_LIMIT) -> str:
    """Начало пользовательского текста для записи в лог (nit n8).

    Длинные запросы не нужны диагностике, но раздувают лог-файлы: пишем не
    более `limit` символов. На обработку запроса не влияет — используется
    ТОЛЬКО в сообщениях логов.
    """
    return text[:limit]


def build_onboarding_keyboard() -> InlineKeyboardMarkup:
    """Inline-клавиатура приветствия (B1): две кнопки мгновенной ценности.

    Подписи проходят truncate_button_text — лимит 64 символа Bot API
    гарантирован конструктором, а не «на глаз».
    """
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            truncate_button_text(RANDOM_MOVIE_BUTTON_TEXT),
            callback_data=RANDOM_MOVIE_CALLBACK,
        ),
        InlineKeyboardButton(
            truncate_button_text(MOOD_BUTTON_TEXT),
            callback_data=MOOD_START_CALLBACK,
        ),
    ]])


def classify_error(exc: Optional[BaseException]) -> ErrorClass:
    """Класс ошибки для дружелюбного ответа (B7): network / llm / generic.

    Порядок проверок — от специфичного к общему (design.md D2):
    1. текстовые маркеры LLM-провайдеров (GigaChat/DeepSeek/токен/
       классификация). `gigachat_client.is_transient_error` намеренно НЕ
       используется как признак LLM: он ловит любые aiohttp-ошибки и
       присвоил бы класс LLM сетевым сбоям Kinopoisk;
    2. ошибки доставки Telegram: `NetworkError` (покрывает `TimedOut`) —
       тот же выход «повторить»;
    3. временные ошибки сети/Kinopoisk API (`is_kinopoisk_transient_error`:
       aiohttp-ошибки, таймауты, HTTP 429/5xx);
    4. прочее — резервный класс (ответ тоже содержит действия-кнопки).

    `None` означает «исключения нет, но результат пуст» (движок рекомендаций
    глотает сбои API и возвращает []) — трактуется как класс сети/Kinopoisk.
    """
    if exc is None:
        return ERROR_CLASS_NETWORK
    message = str(exc).lower()
    if any(marker in message for marker in _LLM_ERROR_MARKERS):
        return ERROR_CLASS_LLM
    if isinstance(exc, NetworkError):
        return ERROR_CLASS_NETWORK
    if is_kinopoisk_transient_error(exc):
        return ERROR_CLASS_NETWORK
    return ERROR_CLASS_GENERIC


def build_error_keyboard(error_class: ErrorClass, retry_target: Optional[str] = None) -> InlineKeyboardMarkup:
    """Клавиатура выхода для класса ошибки (B7): всегда ≥1 действие.

    `retry_target` добавляет кнопку «🔄 Повторить» (`retry:{target}`);
    для класса сети вторая кнопка — «🎲 Случайный фильм» (исключение — цель
    повтора `random`, см. ниже), для LLM и резервного класса — валидные
    примеры действий («🏆 Топ комедий», «🎲 Случайный фильм»).

    При цели повтора `random` кнопка «🎲 Случайный фильм» делала бы ровно то
    же, что «🔄 Повторить» (гайдлайн B6: один эмодзи — один смысл, две кнопки
    не должны вести в одну точку), поэтому вторым выходом в этом случае
    становится «🏆 Топ комедий» (`retry:top`): два РАЗНЫХ действия.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if retry_target:
        rows.append([InlineKeyboardButton(
            truncate_button_text(_RETRY_BUTTON_TEXT),
            callback_data=f"{_RETRY_PREFIX}{retry_target}",
        )])
    top_button = InlineKeyboardButton(
        truncate_button_text(_EXIT_TOP_BUTTON_TEXT),
        callback_data=f"{_RETRY_PREFIX}top",
    )
    random_button = InlineKeyboardButton(
        truncate_button_text(_EXIT_RANDOM_BUTTON_TEXT),
        callback_data=RANDOM_MOVIE_CALLBACK,
    )
    if error_class == ERROR_CLASS_NETWORK:
        # Сетевой класс: один выход-пример (повтор уже в первом ряду)
        rows.append([top_button] if retry_target == 'random' else [random_button])
    elif retry_target == 'random':
        rows.append([top_button])
    else:
        rows.append([top_button, random_button])
    return InlineKeyboardMarkup(rows)


def build_error_reply(
    exc: Optional[BaseException],
    retry_target: Optional[str] = None,
    error_class: Optional[ErrorClass] = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Пара «текст ошибки + клавиатура выхода» для любого пути сбоя (B7).

    `error_class` позволяет задать класс явно (например, пустой результат
    поиска или не-200 от Kinopoisk — класс сети без исключения);
    иначе класс определяется `classify_error`. Тексты — константы модуля,
    внешние данные не подставляются (экранирование не требуется).
    """
    resolved_class = error_class or classify_error(exc)
    if resolved_class == ERROR_CLASS_NETWORK:
        text = _ERROR_TEXT_NETWORK
    elif resolved_class == ERROR_CLASS_LLM:
        text = _ERROR_TEXT_LLM
    else:
        text = _ERROR_TEXT_GENERIC
    return text, build_error_keyboard(resolved_class, retry_target)


def build_exit_keyboard() -> InlineKeyboardMarkup:
    """Универсальные кнопки выхода: валидные примеры мгновенных действий.

    Состав кнопок берётся из `build_error_keyboard` (nit n1: один источник
    формы, без дублирования) — резервный класс без цели повтора даёт ряд
    «🏆 Топ комедий» (`retry:top`) + «🎲 Случайный фильм» (`random:movie`).
    """
    return build_error_keyboard(ERROR_CLASS_GENERIC)


def build_unknown_callback_keyboard() -> InlineKeyboardMarkup:
    """Кнопки выхода для неизвестного/устаревшего callback (B7, nit n2).

    Переиспользуем существующие маршруты: «⬅️ К списку» (`back:list`) и
    «🎲 Случайный фильм» (`random:movie`) — новых сущностей нет, тупика нет
    даже из старой карточки (design.md D4).
    """
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            truncate_button_text(_EXIT_BACK_BUTTON_TEXT),
            callback_data='back:list',
        ),
        InlineKeyboardButton(
            truncate_button_text(_EXIT_RANDOM_BUTTON_TEXT),
            callback_data=RANDOM_MOVIE_CALLBACK,
        ),
    ]])


def build_watchlist_open_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура ответа на сохранение (B4): переход к «📌 Мой список».

    Единственная кнопка — мгновенное действие-выход (B7): после «✅
    Сохранено» / «Уже в списке» пользователь открывает список одним тапом.
    """
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        truncate_button_text(WATCHLIST_OPEN_BUTTON_TEXT),
        callback_data=WATCHLIST_CALLBACK,
    )]])


def get_main_menu():
    keyboard = [
        [KeyboardButton("🎭 Фильм по настроению"), KeyboardButton("🏆 Топ фильмов")],
        [KeyboardButton("🎬 Поиск по жанру"), KeyboardButton(WATCHLIST_MENU_BUTTON_TEXT)],
        [KeyboardButton("🔄 Другие варианты"), KeyboardButton("💡 Помощь")],
        [KeyboardButton("🆕 Новый диалог")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def get_top_menu():
    keyboard = [
        [KeyboardButton("🏆 Топ 50 фильмов"), KeyboardButton(f"🏆 Топ фильмов {CURRENT_YEAR}")],
        [KeyboardButton("📺 Топ 50 сериалов"), KeyboardButton(f"📺 Топ сериалов {CURRENT_YEAR}")],
        [KeyboardButton("⬅️ Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Онбординг «ценность сразу» (B1): одно короткое сообщение + кнопки.

    РОВНО ОДНО сообщение не более 4 строк с inline-клавиатурой мгновенных
    действий («🎲 Случайный фильм вечера», «🎭 Подобрать по настроению») —
    ценность в одно нажатие, без обучения. В текстах — только эмодзи с
    закреплённым смыслом из гайдлайна (docs/emoji_guideline.md, B6).
    Reply-меню к /start больше не прикрепляется (одно сообщение не несёт
    два reply_markup); `get_main_menu` и диспетчеризация по тексту
    reply-кнопок сохраняются до B8 — их перевод на inline не наша задача.
    """
    await update.message.reply_text(
        _WELCOME_HTML,
        parse_mode='HTML',
        reply_markup=build_onboarding_keyboard(),
    )

async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "💡 <b>Примеры запросов:</b>\n"
        "• <i>По настроению:</i>\n"
        "  «мне грустно», «хочу что-то веселое»\n"
        "  «посоветуй фильм когда устал»\n"
        "• <i>Топы и рейтинги:</i>\n"
        "  «топ комедий», «лучшие фильмы 2020-х»\n"
        "  «популярные триллеры»\n"
        "• <i>Поиск по параметрам:</i>\n"
        "  «комедии с Джимом Керри»\n"
        "  «французские драмы 1990-х»\n"
        "  «фильмы про космос»\n"
        "• <i>Уточнения:</i>\n"
        "  «другие варианты», «похожие фильмы»\n"
        "  «нет, это не то»\n"
        "Я запоминаю контекст нашего разговора!\n"
        "⚠️ Отвечаю только на запросы о фильмах и сериалах."
    )
    # Постоянный якорь навигации (minor m2): /start больше не крепит
    # reply-меню (B1, одно сообщение не несёт два reply_markup), поэтому
    # меню возвращается пользователю вместе со справкой. До B8 это точка,
    # где меню гарантированно снова видно. На «ровно одно сообщение» у
    # /start не влияет — справка это отдельная команда.
    await update.message.reply_text(help_text, parse_mode='HTML', reply_markup=get_main_menu())

async def handle_top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выберите категорию топа:", reply_markup=get_top_menu())

async def handle_genre_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Какой жанр вас интересует?\n"
        "Например: комедия, драма, боевик, триллер, ужасы, фантастика, мелодрама, приключения..."
    )

async def handle_mood_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_MOOD_PROMPT_HTML, parse_mode='HTML')

# === Общая доставка ответа в чат (A4) ===

async def _send_typing(target: Any) -> None:
    """Показать статус «печатает…» в чате цели отправки (fail-silent).

    `target` — `update.message` либо `query.message`: у реального
    PTB-объекта есть `.chat`, у тестовых фейков его может не быть, поэтому
    доступ идёт через `getattr`. Сбой статуса не должен прерывать отправку
    ответа пользователю.
    """
    chat = getattr(target, "chat", None)
    send_action = getattr(chat, "send_action", None)
    if send_action is None:
        return
    try:
        await send_action(action="typing")
    except Exception as e:
        logger.warning(f"Не удалось показать статус «печатает…»: {e}")


def _valid_poster_url(result: Dict[str, Any]) -> str:
    """Валидный URL постера из результата диалога либо пустая строка.

    Валидность та же, что и до A4: непустое значение, начинающееся с
    «http» (проверка отсекает и пустой URL, и относительные пути).
    """
    movie = result.get("movie") or {}
    poster_url = str(movie.get("poster_url") or "").strip()
    return poster_url if poster_url.startswith("http") else ""


async def _send_result(target: Any, result: Dict[str, Any], do_quote: Optional[bool] = None) -> None:
    """Отправить результат диалога НОВЫМ сообщением: карточка одним либо текст (A4).

    Единая точка отправки для текстового пути (`_process_and_reply`) и
    fallback'ов edit-доставки A5: `target` — `update.message` или
    `query.message` (у обоих есть `reply_text`/`reply_photo`). При валидном
    постере карточка фильма уходит ОДНИМ `reply_photo` с caption,
    `parse_mode='HTML'` и inline-клавиатурой; без постера или при отказе
    Telegram (битая ссылка) — текстовый fallback с тем же текстом и той же
    разметкой, warning в лог и без падения.

    `do_quote=False` передают fallback'и `_edit_or_send` (A5): новое
    сообщение НЕ ДОЛЖНО цитировать заменяемое/удаляемое — в непубличных
    чатах PTB `_do_quote` по умолчанию подставляет ReplyParameters, а
    ответ на удалённое сообщение Telegram отклоняет («replied message not
    found») — возник бы dead-end. `None` — прежнее поведение (цитата в
    группах по умолчанию PTB).

    Callback-маршруты доставляют результат через `_edit_or_send` (A5):
    редактирование сообщения вместо нового, а `_send_result` остаётся
    отправителем нового сообщения (обычный текстовый путь и fallback).
    """
    text = result.get("response") or ""
    reply_markup = result.get("reply_markup")
    poster_url = _valid_poster_url(result)
    if poster_url:
        try:
            await target.reply_photo(
                photo=poster_url,
                caption=text,
                parse_mode='HTML',
                reply_markup=reply_markup,
                do_quote=do_quote
            )
            return
        except Exception as photo_err:
            logger.warning(f"Не удалось отправить постер — карточка текстом: {photo_err}")
    await target.reply_text(text, parse_mode='HTML', reply_markup=reply_markup, do_quote=do_quote)


# === Доставка callback-результата редактированием сообщения (A5) ===

# Маркер ошибки Telegram «Message is not modified: specified new message
# content and reply markup are exactly the same as a current content of the
# message»: повторный тап по той же кнопке — штатная ситуация, а не ошибка
# (дельта-спека callback-routing: молча игнорировать). Короткая подстрока —
# сравнение меньше зависит от формулировки сервера (ревью n2).
_NOT_MODIFIED_MARKER = 'not modified'


def _is_not_modified_error(error: BadRequest) -> bool:
    """True, если BadRequest — «message is not modified» (повторный тап, A5).

    Сравнение регистронезависимое по тексту ошибки сервера; прочие
    BadRequest («message can't be edited», «message to edit not found»,
    «message can't be deleted») признаком НЕ считаются — они ведут на
    fallback новым сообщением.
    """
    return _NOT_MODIFIED_MARKER in (error.message or '').lower()


async def _delete_message_quietly(message: Any) -> None:
    """Удалить сообщение fail-silent (A5, замена сообщения в fallback'ах).

    Удаление запрещено для сообщений старше 48 часов либо при отсутствии
    прав — это не фатально: новое сообщение УЖЕ доставлено (порядок
    «сначала отправка, затем удаление» — контент не теряется при сбое),
    в лог пишется warning. Отсутствие метода `delete` (фейки) — тоже
    штатная ситуация.
    """
    delete = getattr(message, 'delete', None)
    if delete is None:
        return
    try:
        await delete()
    except Exception as e:
        logger.warning(f"Не удалось удалить заменённое сообщение: {e}")


async def _edit_or_send(message: Any, result: Dict[str, Any]) -> None:
    """Доставить callback-результат РЕДАКТИРОВАНИЕМ сообщения; fallback — новое (A5).

    Единая точка edit-доставки для всех callback-маршрутов (`info:`,
    `alt:`, `similar:`, `back:`): в штатном пути новых сообщений в чате не
    появляется, история остаётся чистой. Матрица переходов (design.md D2),
    форма результата — по `_valid_poster_url`, форма сообщения — по
    `message.photo`:

    - текст → текст: `edit_text` (список→список, текстовая карточка→список);
    - ЛЮБОЕ → фото: `edit_media` с `InputMediaPhoto` — установленный PTB
      22.5 (Bot API 9.2) документирует editMessageMedia в том числе как
      «add media to text messages», поэтому список→карточка — тоже одно
      редактирование, идемпотентное (двойной тап даёт «not modified», а не
      дубликат) и не теряющее контент;
    - медиа → текст: `editMessageText` применим только к текстовым и
      игровым сообщениям — фото-карточка в список редактированием НЕ
      превращается. Детерминированный путь: новое сообщение (без цитаты,
      `do_quote=False`), ЗАТЕМ удаление исходного: обречённый вызов
      edit_text и warning на штатном пути исключены, при сбое отправки
      исходная карточка цела, нетто — одно сообщение вместо одного.

    `BadRequest` «message is not modified» (повторный тап) игнорируется
    молча — `answer()` уже выполнен диспетчером ДО доставки. Прочий
    `BadRequest` редактирования (отказ сервера, старое сообщение >48 ч) —
    warning и fallback: СНАЧАЛА новое сообщение (`do_quote=False` — цитата
    удаляемого недопустима), ЗАТЕМ попытка удаления исходного: контент не
    теряется, история по возможности остаётся чистой. Иные исключения
    (сеть/таймауты) всплывают в существующие `except` маршрутов — контур
    отказоустойчивости A3/A4 не дублируется.

    Gate `isinstance(message, Message)`: редактирование включается только
    для реальных PTB-сообщений; тестовые фейки без edit-методов и
    `InaccessibleMessage` (очень старые callback'и) идут прежним путём
    `_send_result` — обратная совместимость доставки.
    """
    if not isinstance(message, Message):
        await _send_result(message, result)
        return
    text = result.get("response") or ""
    reply_markup = result.get("reply_markup")
    poster_url = _valid_poster_url(result)
    if not poster_url and message.photo:
        # Медиа → текст (переход №3): редактирование неприменимо —
        # сначала отправляем (контент не теряется), затем удаляем исходное
        await _send_result(message, result, do_quote=False)
        await _delete_message_quietly(message)
        return
    try:
        if poster_url:
            # Любое → фото (переход №2): edit_media заменяет медиа фото-
            # сообщения И добавляет медиа к текстовому (Bot API 9.2)
            await message.edit_media(
                media=InputMediaPhoto(media=poster_url, caption=text, parse_mode='HTML'),
                reply_markup=reply_markup,
            )
        else:
            # Текст → текст (переход №1)
            await message.edit_text(text, parse_mode='HTML', reply_markup=reply_markup)
    except BadRequest as e:
        if _is_not_modified_error(e):
            # Повторный тап: содержимое уже актуально — молча игнорируем
            return
        logger.warning(f"Не удалось отредактировать сообщение — доставляю новым: {e}")
        # Fallback (переход №4): сначала новое сообщение без цитаты, затем
        # удаление исходного — при сбое отправки контент остаётся на месте
        await _send_result(message, result, do_quote=False)
        await _delete_message_quietly(message)


async def _process_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: str, query: str):
    track_client_request(f"tg:{user_id}")
    # Последний запрос сохраняется для кнопки «🔄 Повторить» (retry:last,
    # B7). In-memory: после рестарта/в другом воркере контекст теряется —
    # повтор даёт дружелюбный выход с альтернативными действиями.
    user_data = getattr(context, 'user_data', None) if context is not None else None
    if user_data is not None:
        user_data[_LAST_QUERY_KEY] = query
    try:
        await _send_typing(update.message)
        async with aiohttp.ClientSession() as http_session:
            result = await dialogue_manager.process_message(http_session, user_id, query)
        # Доставка общим отправителем (A4): карточка фильма приходит одним
        # сообщением с постером, список — текстом с компактной клавиатурой
        await _send_result(update.message, result)
    except Exception as e:
        # Логирование сохранено в прежнем объёме; ответ пользователю —
        # классифицированная ошибка с кнопками выхода (B7), без генерика
        logger.error(f"Ошибка обработки запроса {_log_excerpt(query)!r}: {e}", exc_info=True)
        error_text, error_markup = build_error_reply(e, 'last')
        await update.message.reply_text(
            error_text,
            parse_mode='HTML',
            reply_markup=error_markup,
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = sanitize_message(update.message.text or "")
    user_id = str(update.effective_user.id)
    if not user_message:
        await update.message.reply_text("Пожалуйста, введите запрос.")
        return
    # Лимит длины проверяется централизованно в dialogue_manager
    # (guardrails.precheck_message) — действует и для бота, и для веба
    logger.info(f"Сообщение от {user_id}: {user_message}")

    if user_message == "🎭 Фильм по настроению":
        await update.message.reply_text(_MOOD_PROMPT_HTML, parse_mode='HTML')
        return
    elif user_message == "🏆 Топ фильмов":
        await update.message.reply_text("Выберите категорию топа:", reply_markup=get_top_menu())
        return
    elif user_message == "⬅️ Назад":
        await update.message.reply_text("Вернулись в главное меню", reply_markup=get_main_menu())
        return
    elif user_message == "🎬 Поиск по жанру":
        await update.message.reply_text(
            "Какой жанр вас интересует?\n"
            "Например: комедия, драма, боевик, триллер, ужасы, фантастика, мелодрама, приключения..."
        )
        return
    elif user_message == "🔄 Другие варианты":
        # Тот же запрос, что у inline-кнопки под списком (A3) — общая константа
        user_query = OTHER_VARIANTS_QUERY
    elif user_message == "💡 Помощь":
        await handle_help(update, context)
        return
    elif user_message == WATCHLIST_MENU_BUTTON_TEXT:
        # Пункт меню «📌 Мой список» (B4): тот же код, что у команды /list —
        # единая точка рендера первой страницы watchlist
        await handle_watchlist_command(update, context)
        return
    elif user_message == "🆕 Новый диалог":
        dialogue_manager.clear_user_session(user_id)
        context.user_data.clear()
        await update.message.reply_text(
            "🆕 Начинаем новый диалог! История очищена.\nЧто хотите посмотреть?",
            reply_markup=get_main_menu()
        )
        return
    elif user_message == "🏆 Топ 50 фильмов":
        user_query = "топ 50 фильмов"
    elif user_message == f"🏆 Топ фильмов {CURRENT_YEAR}":
        user_query = f"топ фильмов {CURRENT_YEAR}"
    elif user_message == "📺 Топ 50 сериалов":
        user_query = "топ 50 сериалов"
    elif user_message == f"📺 Топ сериалов {CURRENT_YEAR}":
        user_query = f"топ сериалов {CURRENT_YEAR}"
    else:
        user_query = user_message

    await _process_and_reply(update, context, user_id, user_query)

async def handle_movie_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Использование: <code>/movie Название фильма</code>\n"
            "Пример: <code>/movie Начало</code>",
            parse_mode='HTML'
        )
        return
    movie_title = " ".join(context.args)
    user_id = str(update.effective_user.id)
    await _process_and_reply(update, context, user_id, f"расскажи о фильме {movie_title}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Логирование сохранено как раньше; ответ — классифицированная ошибка
    # с кнопками выхода (B7). Повтор как цель не предлагается: контекст
    # операции после глобального сбоя ненадёжен — даём мгновенные действия.
    logger.error(f"Ошибка: {context.error}", exc_info=True)
    if update and update.effective_message:
        error_text, error_markup = build_error_reply(context.error)
        try:
            await update.effective_message.reply_text(
                error_text,
                parse_mode='HTML',
                reply_markup=error_markup,
            )
        except Exception as reply_err:
            # Отказ доставки ответа в error_handler не должен порождать
            # новый виток обработки ошибок — только лог
            logger.error(f"Не удалось отправить ответ об ошибке: {reply_err}", exc_info=True)

# === Inline-callback'и: диспетчер по префиксам (A3/A4) ===

def _callback_user_id(update: Update, query: Any) -> str:
    """User_id инициатора callback'а либо пустая строка.

    Основной источник — `query.from_user`, запасной — `update.effective_user`
    (у тестовых фейков одного из них может не быть). Пустой результат
    означает, что пользователя определить не удалось: маршруты обязаны
    ответить дружелюбно, а не падать (без dead-end).
    """
    user = getattr(query, "from_user", None) or getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    return str(user_id) if user_id is not None else ""


def _user_data(context: Any) -> Any:
    """`context.user_data` либо None: тесты и сбои дают context=None (B3).

    Доступ через getattr — тот же защитный паттерн, что у `_LAST_QUERY_KEY`
    в маршруте повтора (B7): отсутствие контекста не должно ронять
    callback-маршрут.
    """
    return getattr(context, 'user_data', None) if context is not None else None


def _save_list_page(context: Any, offset: int, list_hash: str) -> None:
    """Записать снапшот текущей страницы выдачи в context.user_data (B3).

    Снапшот читает `_handle_back_callback`: «⬅️ К списку» из карточки
    возвращает пользователя на ТУ ЖЕ страницу (design.md D5). Контекст
    недоступен — молча пропускаем: доставка страницы от этого не зависит.
    """
    user_data = _user_data(context)
    if user_data is None:
        return
    user_data[_LIST_PAGE_KEY] = {'offset': offset, 'hash': list_hash}


def _load_list_page(context: Any, current_hash: str) -> int:
    """Offset сохранённой страницы, если снапшот от ТЕКУЩЕЙ выдачи; иначе 0.

    Валидация hash (design.md D2/D5): выдача перезаписана новым подбором —
    снапшот чужой, возврат идёт на первую страницу; то же при потере
    контекста (рестарт/другой воркер) и при мусорном значении offset.
    """
    user_data = _user_data(context)
    if user_data is None:
        return 0
    snapshot = user_data.get(_LIST_PAGE_KEY)
    if not isinstance(snapshot, dict) or snapshot.get('hash') != current_hash:
        return 0
    try:
        return int(snapshot.get('offset') or 0)
    except (TypeError, ValueError):
        return 0


async def _run_dialogue_query(update: Update, query: Any, user_id: str, text: str) -> None:
    """Обработать текстовый запрос, инициированный нажатием кнопки (A3/A5).

    Тот же пайплайн, что и для обычного сообщения: статус «печатает…» в чате
    сообщения с кнопкой, `process_message` и доставка результата
    РЕДАКТИРОВАНИЕМ исходного сообщения (`_edit_or_send`, A5): новый список
    заменяет прежний на месте, новых сообщений в чате не появляется.
    """
    await _send_typing(query.message)
    async with aiohttp.ClientSession() as http_session:
        result = await dialogue_manager.process_message(http_session, user_id, text)
    await _edit_or_send(query.message, result)


async def _callback_failure(query: Any, text: str, reply_markup: Any = None) -> None:
    """Дружелюбный ответ при сбое callback-маршрута: без dead-end и молчания.

    `reply_markup` — inline-клавиатура с действиями выхода (B7: каждое
    сообщение об ошибке содержит ≥1 действие). Без параметра сохраняется
    прежнее поведение — reply-меню (для НЕ-ошибочных служебных ответов
    вроде «Не удалось определить пользователя…»: там следующим шагом
    является текстовый ввод, а меню уже показано у пользователя; два
    reply_markup в одном сообщении Telegram не поддерживает).
    """
    markup = reply_markup if reply_markup is not None else get_main_menu()
    try:
        await query.message.reply_text(text, parse_mode='HTML', reply_markup=markup)
    except Exception as e:
        logger.error(f"Не удалось отправить ответ на callback: {e}", exc_info=True)


async def _handle_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, query: Any, data: str) -> None:
    """Карточка фильма по callback `info:{id}` (A4: одно сообщение + кнопки).

    A5: карточка доставляется РЕДАКТИРОВАНИЕМ сообщения списка
    (`_edit_or_send`) — при валидном постере текстовый список
    редактируется в фото-карточку (`edit_media` добавляет медиа к
    текстовому сообщению, Bot API 9.2), без постера — в текстовую
    карточку (`edit_text`); в штатном пути новых сообщений в чате не
    появляется, при отказе редактирования — fallback новым сообщением.
    """
    try:
        movie_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError) as e:
        # Разбор callback_data — ОТДЕЛЬНЫЙ try с ранним выходом (nit n3,
        # паттерн `_handle_similar_callback`): в общем обработчике ниже
        # `movie_id` гарантированно определена и не берётся из try-блока.
        logger.warning(f"Некорректный id фильма в callback_data: {e}")
        await _callback_failure(query, "Не удалось определить фильм.", build_exit_keyboard())
        return
    try:
        async with aiohttp.ClientSession() as http_session:
            kinopoisk_client = dialogue_manager.movie_agent.kinopoisk_client
            url = f"{kinopoisk_client.base_url}/{movie_id}"
            async with http_session.get(url, headers=kinopoisk_client.headers) as resp:
                if resp.status == 200:
                    raw = await resp.json()
                    genres = ', '.join([g['name'] for g in raw.get('genres', []) if g.get('name')])
                    countries = ', '.join([c['name'] for c in raw.get('countries', []) if c.get('name')])
                    rating_imdb = raw.get('rating', {}).get('imdb')
                    rating_kp = raw.get('rating', {}).get('kp')
                    poster_url = (raw.get('poster', {}).get('url') or '').strip()
                    movie = {
                        'id': raw.get('id'),
                        'title': raw.get('name') or '—',
                        'year': raw.get('year'),
                        'genre': genres,
                        'country': countries,
                        'rating': rating_imdb or rating_kp or '—',
                        'rating_imdb': rating_imdb,
                        'rating_kp': rating_kp,
                        # Описание не режется «на глаз»: бюджет caption (1024)
                        # обеспечивает единая сборка карточки в обоих путях (A4)
                        'description': raw.get('description') or '',
                        'poster_url': poster_url,
                        # Ссылка на Кинопоиск строится так же, как в движке
                        # выдачи (recommendation_engine): без неё у карточки
                        # из callback не было бы кнопки «🔗 Кинопоиск» (A4)
                        'kinopoisk_url': f"https://www.kinopoisk.ru/film/{raw.get('id')}/" if raw.get('id') else None,
                        # IMDb ID из externalId (фаза 1, B3): полный
                        # документ фильма содержит его без selectFields
                        'imdb_id': extract_imdb_id(raw)
                    }
                    # Обогащение карточки RT-оценками (фаза 1, B5): данные
                    # для бейджа B7; fail-silent — при сбое источника или
                    # кэша карточка отправляется без оценок
                    movie = (await enrich_movies_with_rt_scores(http_session, [movie]))[0]
                    # Карточка собирается ЕДИНОЙ функцией (B7/A4) и
                    # доставляется редактированием сообщения (A5)
                    card_text, card_markup = build_movie_card(movie)
                    await _edit_or_send(query.message, {
                        "response": card_text,
                        "reply_markup": card_markup,
                        "movie": movie
                    })
                else:
                    # Не-200 от API (minor m3): статус всегда логируем и
                    # различаем классы. Временный сбой (429/5xx) — класс
                    # «сеть/Kinopoisk» с повтором загрузки ЭТОЙ карточки
                    # (retry:info:{id}); постоянный отказ (прочие 4xx:
                    # 401/403/404 …) — повтор заведомо безуспешен, поэтому
                    # текст иной и кнопки уводят на другие действия (B7:
                    # ≥1 действие сохранено).
                    logger.warning(f"Kinopoisk вернул HTTP {resp.status} для фильма {movie_id}")
                    if resp.status in _KINOPOISK_RETRYABLE_STATUSES:
                        error_text, error_markup = build_error_reply(
                            None, f"info:{movie_id}", error_class=ERROR_CLASS_NETWORK
                        )
                    else:
                        error_text, error_markup = (
                            _ERROR_TEXT_MOVIE_UNAVAILABLE,
                            build_exit_keyboard(),
                        )
                    await _callback_failure(query, error_text, error_markup)
    except (ValueError, IndexError, KeyError) as e:
        # Ошибка разбора данных ответа API — штатная ситуация
        # (отказоустойчивость callback-маршрутов): объяснение +
        # действие-выход вместо голого текста
        logger.warning(f"Ошибка обработки callback_data: {e}")
        await _callback_failure(query, "Не удалось определить фильм.", build_exit_keyboard())
    except Exception as e:
        # Сеть/сбой API не должны оставлять пользователя без ответа:
        # классифицированная ошибка (B7) с повтором загрузки карточки
        logger.error(f"Ошибка загрузки карточки фильма по callback: {e}", exc_info=True)
        error_text, error_markup = build_error_reply(e, f"info:{movie_id}")
        await _callback_failure(query, error_text, error_markup)


async def _handle_alternative_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, query: Any, data: str) -> None:
    """Кнопка «🔄 Другие варианты» под списком (A3) — alternative-интент.

    Поведение то же, что у reply-кнопки с таким же текстом: запрос
    `OTHER_VARIANTS_QUERY` обрабатывается `dialogue_manager`, а результат
    доставляется `_edit_or_send` (A5) — новый список РЕДАКТИРУЕТ прежний
    на месте (текст→текст); `_send_result` новым сообщением остаётся
    только fallback'ом при отказе редактирования. Клик учитывается в
    клиентской статистике по аналогии с текстовым запросом (метрика
    активности по inline-кнопкам).
    """
    user_id = _callback_user_id(update, query)
    if not user_id:
        await _callback_failure(query, f"Не удалось определить пользователя. Напишите «{OTHER_VARIANTS_QUERY}» текстом — подберу варианты.")
        return
    track_client_request(f"tg:{user_id}")
    try:
        await _run_dialogue_query(update, query, user_id, OTHER_VARIANTS_QUERY)
    except Exception as e:
        # Классифицированная ошибка (B7) с повтором той же операции (retry:alt)
        logger.error(f"Ошибка обработки callback «другие варианты»: {e}", exc_info=True)
        error_text, error_markup = build_error_reply(e, 'alt')
        await _callback_failure(query, error_text, error_markup)


async def _handle_similar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, query: Any, data: str) -> None:
    """Кнопка «🎬 Похожие» в карточке (A4) — тонкая обёртка similar-интента.

    Фильм ищется в `session.last_movies` по id из callback'а; подбор
    выполняет существующий similar-пайплайн (`find_similar_by_id`), новый
    пайплайн не создаётся и LLM-классификатор не вызывается. A5: список
    похожих доставляется `_edit_or_send`. Из ТЕКСТОВОЙ карточки — это
    редактирование на месте (текст→текст); из ФОТО-карточки медиа в текст
    редактированием не превращается, поэтому список приходит новым
    сообщением (без цитаты), а карточка удаляется — нетто одно сообщение,
    история чистая.
    """
    user_id = _callback_user_id(update, query)
    if not user_id:
        await _callback_failure(query, "Не удалось определить пользователя. Напишите «посоветуй похожие» текстом — подберу варианты.")
        return
    track_client_request(f"tg:{user_id}")
    try:
        movie_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError) as e:
        # Разбор callback_data — штатная ситуация: объяснение + выход (B7)
        logger.warning(f"Некорректный id фильма в callback_data: {e}")
        await _callback_failure(query, "Не удалось определить фильм.", build_exit_keyboard())
        return
    try:
        await _send_typing(query.message)
        async with aiohttp.ClientSession() as http_session:
            result = await dialogue_manager.find_similar_by_id(http_session, user_id, movie_id)
        await _edit_or_send(query.message, result)
    except Exception as e:
        # Классифицированная ошибка (B7) с повтором подбора (retry:similar:{id})
        logger.error(f"Ошибка обработки callback «похожие»: {e}", exc_info=True)
        error_text, error_markup = build_error_reply(e, f"similar:{movie_id}")
        await _callback_failure(query, error_text, error_markup)


async def _handle_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, query: Any, data: str) -> None:
    """Кнопка «⬅️ К списку» в карточке (A5: обратное редактирование).

    Список пересобирается из `session.last_movies` общим рендерером
    (тот же текст и та же клавиатура, что и при обычной выдаче) и
    доставляется `_edit_or_send`: список возвращается НА МЕСТО. Из
    ТЕКСТОВОЙ карточки — редактирование на месте (текст→текст); из
    ФОТО-карточки медиа в текст редактированием не превращается, поэтому
    список приходит новым сообщением (без цитаты), а карточка удаляется —
    нетто одно сообщение, история чистая.

    B3 (design.md D5): возврат восстанавливает ТУ ЖЕ страницу выдачи —
    снапшот (offset + hash) берётся из `context.user_data` и применяется
    только при совпадении hash с текущей выдачей; список сменился,
    снапшот потерян (рестарт/другой воркер) или контекст недоступен —
    показывается первая страница (без ошибки). Источник списка остаётся
    единым — `session.last_movies`; фактическое состояние страницы
    записывается обратно в снапшот (кнопки восстановленной страницы
    получают свежий hash).
    """
    user_id = _callback_user_id(update, query)
    if not user_id:
        await _callback_failure(query, "Не удалось определить пользователя. Напишите запрос текстом — подберу фильмы заново.")
        return
    try:
        # Чтение сессии может ходить в PostgreSQL — не блокируем event loop
        session = await asyncio.to_thread(dialogue_manager.session_manager.get_session, user_id)
        movies = session.last_movies or []
        if not movies:
            # Список не сохранился (рестарт/истечение сессии) — дружелюбный
            # выход вместо пустого списка или ошибки (без dead-end). Сервисный
            # ответ с reply-меню отправляется НОВЫМ сообщением (design.md D4):
            # редактировать в него карточку вредно — теряется контент выдачи.
            await query.message.reply_text(
                "Список не сохранился — возможно, сессия обновилась.\n"
                "Напишите, что хотите посмотреть, и я подберу заново.",
                parse_mode='HTML',
                reply_markup=get_main_menu()
            )
            return
        current_hash = compute_list_hash(movies)
        offset = clamp_page_offset(len(movies), _load_list_page(context, current_hash))
        text, markup = dialogue_manager.render_movie_list(movies, BACK_TO_LIST_HEADER, offset=offset)
        await _edit_or_send(query.message, {"response": text, "reply_markup": markup})
        _save_list_page(context, offset, current_hash)
    except Exception as e:
        # Классифицированная ошибка (B7) с повтором возврата (retry:back)
        logger.error(f"Ошибка обработки callback «к списку»: {e}", exc_info=True)
        error_text, error_markup = build_error_reply(e, 'back')
        await _callback_failure(query, error_text, error_markup)


async def _handle_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, query: Any, data: str) -> None:
    """Кнопка «⬇️ Ещё 5» под списком (B3): следующая страница выдачи.

    Источник среза — `session.last_movies` (полная выдача до 13 фильмов;
    переживает >45 с, в отличие от in-memory кэша движка `_search_cache` с
    TTL ~45 с — ловушка AGENTS.md, критерий приёмки B3). Рендер — общий
    `render_movie_list` с offset, доставка — `_edit_or_send` (A5):
    страница ЗАМЕНЯЕТ список на месте, нового сообщения нет. Повторный тап
    идемпотентен: рендер детерминирован, «message is not modified»
    гасится молча, offset не сбивается.

    `{hash}` в callback — отпечаток выдачи (`compute_list_hash`): кнопка
    устарела (список перезаписан новым подбором) или сессия пуста —
    дружелюбный ответ с выходами «⬅️ К списку» / «🎲 Случайный фильм»
    (B7), сообщение списка не редактируется. Успешная доставка пишет
    снапшот страницы в `context.user_data` — «⬅️ К списку» из карточки
    вернёт на эту же страницу (design.md D5). Клик учитывается в
    клиентской статистике; сбой — классифицированная ошибка с повтором
    `retry:page:{offset}:{hash}` (та же страница).
    """
    try:
        # Разбор callback_data — отдельный try с ранним выходом (паттерн
        # `_handle_info_callback`): в основном блоке offset/page_hash
        # гарантированно определены
        parts = data.split(':')
        offset = int(parts[1])
        page_hash = parts[2]
    except (ValueError, IndexError) as e:
        logger.warning(f"Некорректные параметры страницы в callback_data: {e}")
        await _callback_failure(query, "Не удалось открыть страницу списка.", build_exit_keyboard())
        return
    user_id = _callback_user_id(update, query)
    if not user_id:
        await _callback_failure(query, "Не удалось определить пользователя. Напишите запрос текстом — подберу фильмы заново.")
        return
    # Учёт клика ДО валидации hash/сессии — осознанно (ревью n3): метрика
    # считает обращения клиента, а не успешные выдачи, и прочие маршруты
    # кнопок (alt:/random:/similar:) пишут её в той же точке; перенос внутрь
    # try сделал бы страницу единственным «условно учитываемым» маршрутом.
    track_client_request(f"tg:{user_id}")
    try:
        # Чтение сессии может ходить в PostgreSQL — не блокируем event loop
        session = await asyncio.to_thread(dialogue_manager.session_manager.get_session, user_id)
        movies = session.last_movies or []
        current_hash = compute_list_hash(movies)
        if not movies or current_hash != page_hash:
            # Кнопка устарела (выдача перезаписана) либо список не сохранился:
            # объяснение + выходы (B7), чужой срез не показываем
            logger.info(f"Устаревший callback пагинации: hash={page_hash!r}, текущий={current_hash!r}, фильмов={len(movies)}")
            await _callback_failure(query, _ERROR_TEXT_STALE_PAGE, build_unknown_callback_keyboard())
            return
        applied_offset = clamp_page_offset(len(movies), offset)
        # «Рукодельный» callback с валидным hash, но offset за границами
        # выдачи клампится в 0 (design.md D3): первая страница получает
        # НЕЙТРАЛЬНЫЙ заголовок — «Продолжение списка:» на offset=0
        # семантически неверно (ревью m1).
        header = PAGE_CONTINUATION_HEADER if applied_offset else BACK_TO_LIST_HEADER
        text, markup = dialogue_manager.render_movie_list(movies, header, offset=applied_offset)
        await _edit_or_send(query.message, {"response": text, "reply_markup": markup})
        _save_list_page(context, applied_offset, current_hash)
    except Exception as e:
        # Классифицированная ошибка (B7): «🔄 Повторить» повторяет ТУ ЖЕ
        # страницу (retry:page:{offset}:{hash} ≤ 64 байт)
        logger.error(f"Ошибка обработки callback пагинации: {e}", exc_info=True)
        error_text, error_markup = build_error_reply(e, f"{PAGE_CALLBACK_PREFIX}{offset}:{page_hash}")
        await _callback_failure(query, error_text, error_markup)


async def _handle_random_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, query: Any, data: str) -> None:
    """Кнопка «🎲 Случайный фильм вечера» (B1): мгновенная карточка без ввода.

    Один вызов движка рекомендаций (`DialogueManager.get_random_movie`,
    без LLM-классификатора) и доставка карточки `_edit_or_send` (A5):
    сообщение приветствия заменяется фото-карточкой, без постера —
    текстовой карточкой (fallback). Пустой результат или сбой — ошибка
    класса «сеть/Kinopoisk» с кнопкой «🔄 Повторить» (`retry:random`, B7);
    логирование сохранено. Клик учитывается в клиентской статистике.
    """
    user_id = _callback_user_id(update, query)
    if not user_id:
        await _callback_failure(query, "Не удалось определить пользователя. Напишите запрос текстом — подберу фильм.")
        return
    track_client_request(f"tg:{user_id}")
    try:
        await _send_typing(query.message)
        async with aiohttp.ClientSession() as http_session:
            result = await dialogue_manager.get_random_movie(http_session, user_id)
        if result is None:
            # Движок глотает сбои API и возвращает [] — отвечаем классом
            # «сеть/Kinopoisk» с повтором (B7: пустой результат не тупик)
            logger.warning(f"Случайный фильм: поиск не дал результатов для {user_id}")
            error_text, error_markup = build_error_reply(None, 'random')
            await _callback_failure(query, error_text, error_markup)
            return
        await _edit_or_send(query.message, result)
    except Exception as e:
        logger.error(f"Ошибка обработки callback «случайный фильм»: {e}", exc_info=True)
        error_text, error_markup = build_error_reply(e, 'random')
        await _callback_failure(query, error_text, error_markup)


async def _handle_mood_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, query: Any, data: str) -> None:
    """Кнопка «🎭 Подобрать по настроению» из онбординга (B1).

    Тот же текст приглашения, что у команды /mood (единый источник
    `_MOOD_PROMPT_HTML` — DRY). Отправляется НОВЫМ сообщением: кнопки
    онбординга под приветствием сохраняются, повторный тап не лишает
    пользователя выбора. LLM не вызывается, dead-end исключён.

    Сбой доставки — ошибка с целью повтора `mood` (minor m4, design.md D3):
    «🔄 Повторить» повторяет ТУ ЖЕ операцию, а не случайный фильм.
    """
    try:
        await query.message.reply_text(_MOOD_PROMPT_HTML, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Ошибка обработки callback «по настроению»: {e}", exc_info=True)
        error_text, error_markup = build_error_reply(e, 'mood')
        await _callback_failure(query, error_text, error_markup)


async def _handle_retry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, query: Any, data: str) -> None:
    """Кнопка «🔄 Повторить» (B7): повтор операции по цели `retry:{target}`.

    Цели: `last` — последний текстовый запрос (in-memory
    `context.user_data`; при потере контекста — дружелюбный выход с
    альтернативными действиями, без падения), `random` — повторный
    случайный фильм, `mood` — повтор приглашения «по настроению»,
    `top` — запрос «топ комедий», `alt`/`back` — повтор соответствующего
    маршрута, `info:{id}`/`similar:{id}`/`page:{offset}:{hash}` — повтор с
    восстановленным callback_data (≤64 байт). Результат доставляется общими
    правилами маршрута (`_edit_or_send`); сбой повтора классифицируется тем
    же контуром, рекурсивного падения нет.
    """
    target = data.split(':', 1)[1] if ':' in data else ''
    if target == 'random':
        await _handle_random_callback(update, context, query, data)
        return
    if target == 'mood':
        # minor m4: повтор приглашения «по настроению» — та же операция,
        # которую предлагала кнопка «🔄 Повторить» (design.md D3)
        await _handle_mood_callback(update, context, query, MOOD_START_CALLBACK)
        return
    if target == 'alt':
        await _handle_alternative_callback(update, context, query, 'alt:list')
        return
    if target == 'back':
        await _handle_back_callback(update, context, query, 'back:list')
        return
    if target.startswith('info:') or target.startswith('similar:'):
        if target.startswith('info:'):
            await _handle_info_callback(update, context, query, target)
        else:
            await _handle_similar_callback(update, context, query, target)
        return
    if target.startswith(PAGE_CALLBACK_PREFIX):
        # B3: повтор страницы пагинации — target уже содержит полные
        # `page:{offset}:{hash}`, разбор повторится в маршруте
        await _handle_page_callback(update, context, query, target)
        return
    if target.startswith(SAVE_CALLBACK_PREFIX) or target.startswith(UNSAVE_CALLBACK_PREFIX):
        # B4: повтор сохранения/удаления — target уже содержит полный
        # callback_data (`save:{id}`/`unsave:{id}`), разбор повторится в маршруте
        if target.startswith(SAVE_CALLBACK_PREFIX):
            await _handle_save_callback(update, context, query, target)
        else:
            await _handle_unsave_callback(update, context, query, target)
        return
    if target == WATCHLIST_CALLBACK or target.startswith(WATCHLIST_PAGE_PREFIX):
        # B4: повтор просмотра списка / страницы списка
        if target == WATCHLIST_CALLBACK:
            await _handle_watchlist_callback(update, context, query, target)
        else:
            await _handle_watchlist_page_callback(update, context, query, target)
        return
    if target in ('last', 'top'):
        user_id = _callback_user_id(update, query)
        if not user_id:
            await _callback_failure(query, "Не удалось определить пользователя. Напишите запрос текстом — подберу фильм.")
            return
        retry_query = _RETRY_TOP_QUERY if target == 'top' else None
        if retry_query is None:
            user_data = getattr(context, 'user_data', None) if context is not None else None
            retry_query = str(user_data.get(_LAST_QUERY_KEY) or '') if user_data is not None else ''
            if not retry_query:
                # Контекст потерян (рестарт/другой воркер) — не тупик:
                # объяснение и мгновенные действия вместо повтора
                await _callback_failure(query, _ERROR_TEXT_LOST_RETRY, build_exit_keyboard())
                return
        track_client_request(f"tg:{user_id}")
        try:
            await _run_dialogue_query(update, query, user_id, retry_query)
        except Exception as e:
            logger.error(f"Ошибка повтора запроса {_log_excerpt(retry_query)!r}: {e}", exc_info=True)
            error_text, error_markup = build_error_reply(e, target)
            await _callback_failure(query, error_text, error_markup)
        return
    # Неизвестная цель повтора — резервный класс с кнопками выхода (B7)
    logger.warning(f"Неизвестная цель повтора в callback_data: {data!r}")
    await _callback_failure(query, _ERROR_TEXT_GENERIC, build_exit_keyboard())


# === Watchlist «📌 Мой список» (фаза 3, B4, add-watchlist) ===

async def _fetch_watchlist_page(user_id: str, offset: int) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура страницы «📌 Мой список» (единый рендер для всех входов).

    Источник — PostgreSQL через `WatchlistManager.list_page` (страница из
    `WATCHLIST_PAGE_LIMIT` элементов + общее количество); БД-запросы
    выполняются ВНЕ event loop (`asyncio.to_thread`, паттерн B3).
    Устаревшая кнопка «⬇️ Ещё 5» (список сократился после удалений и
    offset вышел за конец) — не ошибка и не пустой список: показываем
    первую страницу вместо вводящей в заблуждение заглушки «Список пуст».
    """
    manager = get_watchlist_manager()
    items, total = await asyncio.to_thread(manager.list_page, user_id, offset, WATCHLIST_PAGE_LIMIT)
    if not items and offset > 0 and total > 0:
        offset = 0
        items, total = await asyncio.to_thread(manager.list_page, user_id, 0, WATCHLIST_PAGE_LIMIT)
    return render_watchlist_page(items, offset, total)


async def _handle_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, query: Any, data: str) -> None:
    """Кнопка «📌 Сохранить» в карточке (B4): сохранение фильма в watchlist.

    Данные фильма берутся из `session.last_movies` (персистентная сессия в
    PostgreSQL; повторный запрос к Kinopoisk не нужен — id/название/год/
    постер уже в строке выдачи). Сохранение — `WatchlistManager.add`
    (идемпотентно: уникальность `(user_id, kinopoisk_id)` на уровне БД,
    дубль возвращает False без второй записи). Ответ доставляется
    РЕДАКТИРОВАНИЕМ сообщения карточки (A5): успех — «✅ Сохранено в
    «Мой список»», дубль — «Уже в списке», в обоих случаях с кнопкой
    «📌 Мой список» (`watchlist:view`). Если запись не создалась И фильма
    нет в списке (сбой хранилища) — честная классифицированная ошибка с
    повтором `retry:save:{id}` (B7), а не вводящее в заблуждение «Уже в
    списке».
    """
    try:
        movie_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError) as e:
        # Разбор callback_data — отдельный try с ранним выходом (паттерн
        # `_handle_info_callback`): в основном блоке movie_id определена
        logger.warning(f"Некорректный id фильма в save callback_data: {e}")
        await _callback_failure(query, "Не удалось определить фильм.", build_exit_keyboard())
        return
    user_id = _callback_user_id(update, query)
    if not user_id:
        await _callback_failure(query, "Не удалось определить пользователя. Напишите запрос текстом — подберу фильмы заново.")
        return
    track_client_request(f"tg:{user_id}")
    try:
        session = await asyncio.to_thread(dialogue_manager.session_manager.get_session, user_id)
        movie = next((m for m in (session.last_movies or []) if str(m.get('id')) == str(movie_id)), None)
        if movie is None:
            # Подборка перезаписана/сессия пуста — дружелюбный выход (B7)
            logger.info(f"Фильм {movie_id} не найден в сессии {user_id} для сохранения в watchlist")
            await _callback_failure(query, _ERROR_TEXT_SAVE_NO_MOVIE, build_exit_keyboard())
            return
        manager = get_watchlist_manager()
        poster_url = str(movie.get('poster_url') or '').strip()
        year = movie.get('year')
        added = await asyncio.to_thread(
            manager.add,
            user_id,
            kinopoisk_id=movie_id,
            title=str(movie.get('title') or '—'),
            year=year if isinstance(year, int) else None,
            poster_url=poster_url if poster_url.startswith('http') else None,
        )
        if added:
            response_text = _SAVED_TEXT
        else:
            # Дубль либо сбой БД (add fail-silent) — различаем проверкой:
            # «Уже в списке» честно только когда фильм действительно сохранён
            in_list = await asyncio.to_thread(manager.contains, user_id, movie_id)
            if not in_list:
                logger.warning(f"Фильм {movie_id} не сохранён в watchlist пользователя {user_id} (сбой хранилища)")
                error_text, error_markup = build_error_reply(None, f"save:{movie_id}", error_class=ERROR_CLASS_GENERIC)
                await _callback_failure(query, error_text, error_markup)
                return
            response_text = _ALREADY_SAVED_TEXT
        await _edit_or_send(query.message, {
            "response": response_text,
            "reply_markup": build_watchlist_open_keyboard(),
        })
    except Exception as e:
        logger.error(f"Ошибка обработки callback сохранения в watchlist: {e}", exc_info=True)
        error_text, error_markup = build_error_reply(e, f"save:{movie_id}")
        await _callback_failure(query, error_text, error_markup)


async def _handle_unsave_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, query: Any, data: str) -> None:
    """Кнопка «🗑️ Удалить» у элемента списка (B4): удаление и перерисовка.

    Удаление — `WatchlistManager.remove`: отсутствие записи (устаревшая
    кнопка, повторный тап) — штатная ситуация, не ошибка. После удаления
    сообщение списка редактированием заменяется ПЕРВОЙ страницей: offset
    не кодируется в `unsave:{id}` (короткий callback без устаревшего
    состояния), дальнейшая навигация — кнопкой «⬇️ Ещё 5».
    """
    try:
        movie_id = int(data.split(":", 1)[1])
    except (ValueError, IndexError) as e:
        logger.warning(f"Некорректный id фильма в unsave callback_data: {e}")
        await _callback_failure(query, "Не удалось удалить фильм из списка.", build_exit_keyboard())
        return
    user_id = _callback_user_id(update, query)
    if not user_id:
        await _callback_failure(query, "Не удалось определить пользователя. Напишите запрос текстом — подберу фильмы заново.")
        return
    track_client_request(f"tg:{user_id}")
    try:
        manager = get_watchlist_manager()
        await asyncio.to_thread(manager.remove, user_id, movie_id)
        text, markup = await _fetch_watchlist_page(user_id, 0)
        await _edit_or_send(query.message, {"response": text, "reply_markup": markup})
    except Exception as e:
        logger.error(f"Ошибка обработки callback удаления из watchlist: {e}", exc_info=True)
        error_text, error_markup = build_error_reply(e, f"unsave:{movie_id}")
        await _callback_failure(query, error_text, error_markup)


async def _deliver_watchlist_page(update: Update, query: Any, offset: int) -> None:
    """Доставка страницы watchlist в ответ на callback (общий код входов)."""
    user_id = _callback_user_id(update, query)
    if not user_id:
        await _callback_failure(query, "Не удалось определить пользователя. Напишите запрос текстом — подберу фильмы заново.")
        return
    track_client_request(f"tg:{user_id}")
    try:
        text, markup = await _fetch_watchlist_page(user_id, offset)
        await _edit_or_send(query.message, {"response": text, "reply_markup": markup})
    except Exception as e:
        retry_target = WATCHLIST_CALLBACK if offset == 0 else f"{WATCHLIST_PAGE_PREFIX}{offset}"
        logger.error(f"Ошибка обработки callback списка watchlist: {e}", exc_info=True)
        error_text, error_markup = build_error_reply(e, retry_target)
        await _callback_failure(query, error_text, error_markup)


async def _handle_watchlist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, query: Any, data: str) -> None:
    """Кнопка «📌 Мой список» (B4): первая страница списка (callback `watchlist:view`)."""
    await _deliver_watchlist_page(update, query, 0)


async def _handle_watchlist_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, query: Any, data: str) -> None:
    """Кнопка «⬇️ Ещё 5» списка watchlist (B4): страница `wpage:{offset}`."""
    try:
        offset = int(data.split(":", 1)[1])
    except (ValueError, IndexError) as e:
        logger.warning(f"Некорректный offset страницы watchlist в callback_data: {e}")
        await _callback_failure(query, "Не удалось открыть страницу списка.", build_exit_keyboard())
        return
    await _deliver_watchlist_page(update, query, offset)


async def handle_watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда `/list` и пункт меню «📌 Мой список» (B4): первая страница списка.

    Доставляется НОВЫМ сообщением: это отдельная точка входа, сообщения-
    источника для редактирования нет. Пустой список — дружелюбная заглушка
    с мгновенным действием (B7); сбой хранилища не роняет бота
    (fail-silent `WatchlistManager` — список считается пустым).
    """
    user_id = str(update.effective_user.id)
    try:
        text, markup = await _fetch_watchlist_page(user_id, 0)
        await update.message.reply_text(text, parse_mode='HTML', reply_markup=markup)
    except Exception as e:
        logger.error(f"Ошибка показа списка watchlist по команде: {e}", exc_info=True)
        error_text, error_markup = build_error_reply(e, WATCHLIST_CALLBACK)
        await update.message.reply_text(error_text, parse_mode='HTML', reply_markup=error_markup)


# Маршрут callback'а: (префикс callback_data, обработчик). Таблица
# расширяемая (A3/A4): B8 добавит свои префиксы одной строкой, структура
# диспетчера при этом не меняется. Объявлена после определений хендлеров.
_CallbackRoute = Callable[[Update, ContextTypes.DEFAULT_TYPE, Any, str], Awaitable[None]]

_CALLBACK_ROUTES: tuple[tuple[str, _CallbackRoute], ...] = (
    ("info:", _handle_info_callback),
    ("alt:", _handle_alternative_callback),
    ("similar:", _handle_similar_callback),
    ("back:", _handle_back_callback),
    # B1: случайный фильм вечера и приглашение «по настроению»
    ("random:", _handle_random_callback),
    ("mood:", _handle_mood_callback),
    # B7: повтор операции после ошибки
    ("retry:", _handle_retry_callback),
    # B3: следующая страница списка выдачи («⬇️ Ещё 5»)
    (PAGE_CALLBACK_PREFIX, _handle_page_callback),
    # B4: watchlist «📌 Мой список» — сохранение/удаление фильма,
    # просмотр списка и его пагинация
    (SAVE_CALLBACK_PREFIX, _handle_save_callback),
    (UNSAVE_CALLBACK_PREFIX, _handle_unsave_callback),
    ("watchlist:", _handle_watchlist_callback),
    (WATCHLIST_PAGE_PREFIX, _handle_watchlist_page_callback),
)


async def handle_movie_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Диспетчер inline-callback'ов: маршрутизация по префиксу `callback_data`.

    Имя сохранено для совместимости с регистрацией хендлера и существующими
    тестами; фактически обрабатывает все маршруты списка и карточки
    (A3/A4/B1/B3/B7): `info:` — карточка, `alt:` — другие варианты,
    `similar:` — похожие, `back:` — возврат к списку, `random:` —
    случайный фильм вечера, `mood:` — приглашение «по настроению»,
    `retry:` — повтор операции после ошибки, `page:` — следующая страница
    списка выдачи, `save:`/`unsave:`/`watchlist:`/`wpage:` — операции
    списка «📌 Мой список» (B4). Неизвестный префикс —
    объяснение с кнопками возврата (B7: без dead-end, вместо прежнего
    голого текста-тупика). `answer()` вызывается на любой callback,
    чтобы у пользователя снялась индикация нажатия.
    """
    query = update.callback_query
    await query.answer()
    # У PTB `query.message` — Optional (для очень старых сообщений его может
    # не быть), а все маршруты ниже разыменовывают его безусловно. Выходим
    # раньше, чтобы не получить AttributeError; `answer()` уже снял индикацию.
    if query.message is None:
        return
    data = query.data or ""
    for prefix, handler in _CALLBACK_ROUTES:
        if data.startswith(prefix):
            await handler(update, context, query, data)
            return
    # Неизвестный префикс (старая кнопка, чужой callback) — объяснение и
    # действия-выходы (B7): клавиатура собрана именованным билдером
    # `build_unknown_callback_keyboard` (nit n2), маршруты back:/random:
    await _callback_failure(
        query,
        _ERROR_TEXT_UNKNOWN_CALLBACK,
        build_unknown_callback_keyboard(),
    )

# === Регистрация команд бота в меню клиента Telegram (A1) ===

def build_bot_commands() -> list[BotCommand]:
    """Собрать список команд бота с русскими описаниями.

    Чистая функция — используется и post-init hook'ом, и тестами. Описания
    показываются в меню команд клиента Telegram (лимит 256 символов).
    """
    return [
        # minor m1: описание /start соответствует фактическому поведению —
        # после B1 команда шлёт короткое приветствие с inline-кнопками
        # мгновенной ценности, а reply-меню к ней больше не крепится
        BotCommand("start", "Запустить бота: случайный фильм вечера и подбор по настроению"),
        BotCommand("help", "Показать справку и примеры запросов"),
        BotCommand("movie", "Узнать информацию о фильме: /movie Название"),
        BotCommand("top", "Показать топы фильмов и сериалов"),
        BotCommand("genre", "Подобрать фильм по жанру"),
        BotCommand("mood", "Подобрать фильм по настроению"),
        # B4: просмотр сохранённых фильмов («📌 Мой список»)
        BotCommand("list", "Мой список сохранённых фильмов"),
    ]

async def register_bot_commands(application: Application) -> None:
    """Зарегистрировать команды в меню Telegram при старте приложения.

    Идемпотентно: повторный запуск просто перезаписывает меню. Fail-silent —
    сбой setMyCommands логируется как warning и не роняет старт бота
    (например, если Telegram временно недоступен).
    """
    try:
        await application.bot.set_my_commands(build_bot_commands())
    except Exception as e:
        logger.warning(f"Не удалось зарегистрировать команды бота: {e}")

# === Сборка приложения бота (единая точка регистрации хендлеров) ===

def create_telegram_app() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN не найден в .env")
    application = (
        Application.builder()
        .token(token)
        .post_init(register_bot_commands)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", handle_help))
    application.add_handler(CommandHandler("movie", handle_movie_command))
    application.add_handler(CommandHandler("top", handle_top_command))
    application.add_handler(CommandHandler("genre", handle_genre_command))
    application.add_handler(CommandHandler("mood", handle_mood_command))
    application.add_handler(CommandHandler("list", handle_watchlist_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    application.add_handler(CallbackQueryHandler(handle_movie_detail))
    return application

# === Запуск в режиме polling ===

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN не найден в .env")
    application = create_telegram_app()
    logger.info("Telegram бот запущен в режиме polling...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    main()
