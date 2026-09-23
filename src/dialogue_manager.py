# src/dialogue_manager.py
import asyncio
import hashlib
import html
import os
import logging
import random

from typing import Dict, Any, List, Optional
from movie_agent import MovieAgent
from session_manager import SessionManager, UserSession
from intent_classifier import IntentClassifier
from llm_router import LLMRouter
from config import CURRENT_YEAR
from guardrails import (
    OFFTOPIC_INTENT,
    get_refusal_text,
    log_blocked,
    precheck_message,
    sanitize_message,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

# --- Бейдж Rotten Tomatoes (фаза 1, Epic B, B7) ---
# Эмодзи-гайдлайн (docs/research/research_uiux_telegram.md, рекомендация 13):
# правило «1 эмодзи = 1 смысл» — 🍅 закреплён ИСКЛЮЧИТЕЛЬНО за процентом
# одобрения фильма критиками Rotten Tomatoes (Tomatometer), других значений
# у него в боте нет. Порога «свежести» для показа бейджа намеренно нет:
# число рядом с 🍅 и есть процент одобрения, поэтому «🍅 32%» читается
# корректно, а наличие бейджа определяется только наличием данных.
# Осознанное отклонение от правила «не более 1 эмодзи на строку списка»:
# ⭐ (зрительский рейтинг) и 🍅 (консенсус критиков) — функциональные
# маркеры источника оценки, а не декор, и бейдж появляется только у фильмов
# с RT-данными (обоснование — design.md D4 изменения add-rt-badge).
RT_BADGE_EMOJI = '🍅'
# Разделитель между рейтингом и бейджем: бейдж ставится в конце строки
# списка/предложения карточки — по гайдлайну эмодзи не в середине фразы.
RT_BADGE_SEPARATOR = ' · '
# Шкала Tomatometer — процент одобрения 0–100 (0 — валидное значение).
RT_BADGE_MIN = 0
RT_BADGE_MAX = 100

# --- Список выдачи (фаза 2, A2, изменение add-rich-movie-list-format) ---
# Отображаемый лимит списка: добыча данных остаётся прежней (13/25/30),
# но в ответе показываются только первые LIST_DISPLAY_LIMIT фильмов и
# столько же кнопок «Подробнее». Полный список по-прежнему пишется в
# session.last_movies и movies_list — это данные для следующих шагов
# диалога («похожие», «другие варианты», пагинация B2/B3).
LIST_DISPLAY_LIMIT = 5
# Разделитель частей строки списка (год, жанр/страна, рейтинг) — тот же
# middot, что между рейтингом и RT-бейджем: один источник литерала.
LIST_PART_SEPARATOR = RT_BADGE_SEPARATOR

# --- Компактные inline-кнопки (фаза 2, A3, add-compact-buttons-and-movie-card) ---
# Кнопок-номеров в одном ряду: при LIST_DISPLAY_LIMIT = 5 это два ряда
# (4 + 1) вместо прежнего столбца из пяти кнопок «Подробнее: {title}».
BUTTONS_PER_ROW = 4
# Лимит подписи inline-кнопки по Bot API — 64 символа. Превышение
# отклоняется Telegram (BadRequest), поэтому все подписи, собираемые из
# данных фильма/сессии, проходят truncate_button_text.
BUTTON_TEXT_LIMIT = 64
# Подпись-заполнитель для пустых данных: пустая кнопка недопустима, а
# «Подробнее: None» из ревью A2 исключается конструктивно.
BUTTON_TEXT_EMPTY = '—'
# Эмодзи-цифры подписей кнопок-номеров (1️⃣ … 🔟). Для номера больше
# длины кортежа — обычная десятичная запись числа (задел на рост лимита).
NUMBER_BUTTON_LABELS = ('1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟')
# Ряд навигации ПОД номерами (A3 → B2): контекстные quick replies,
# привязанные к конкретной выдаче, — «другие варианты» (существующий
# alternative-интент), «ещё 5» (пагинация B3) и «случайный фильм»
# (существующий маршрут B1). Подписи укорочены относительно прежней
# «🔄 Другие варианты»: три кнопки с длинными подписами в одном ряду
# Telegram ужимает с обрезкой текста на узких экранах (прецедент —
# CARD_BUTTONS_PER_ROW=2 в A4). Эмодзи по docs/emoji_guideline.md:
# 🔄 — другие варианты, 🎲 — случайный фильм, ⬇️ — пагинация/«показать ещё».
LIST_NAV_BUTTON_TEXT = '🔄 Другие'
LIST_NAV_CALLBACK = 'alt:list'
LIST_MORE_BUTTON_TEXT = '⬇️ Ещё 5'
LIST_RANDOM_BUTTON_TEXT = '🎲 Случайный'
# Callback маршрута случайного фильма (B1) — единый источник литерала
# перенесён из telegram_bot.py: кнопка «🎲 Случайный» ряда навигации
# (B2) собирается здесь; telegram_bot импортирует это имя (онбординг,
# клавиатуры выхода B7 и тесты сохраняют прежнее имя).
RANDOM_MOVIE_CALLBACK = 'random:movie'
# Префикс callback пагинации (B3): полная форма `page:{offset}:{hash}`,
# где offset — стартовая позиция следующей страницы, hash — отпечаток
# выдачи (compute_list_hash). Диспетчер бота маршрутизирует по префиксу.
PAGE_CALLBACK_PREFIX = 'page:'
# Длина отпечатка выдачи в байтах: 4 байта → 8 hex-символов. Callback
# `page:{offset}:{hash}` даже с трёхзначным offset — ≤17 байт, запас до
# лимита Bot API (64 байта) огромный; коллизия в рамках одной сессии
# практически невозможна и неопасна (design.md D2).
LIST_HASH_BYTES = 4
# Явный текст исчерпания сохранённой выдачи (B3, design.md D4): последняя
# страница пагинации НЕ делает новый запрос к движку — сообщает, что
# подходящие фильмы кончились. Без эмодзи: «текст без эмодзи — допустимая
# норма» (гайдлайн), а ⚠️ зарезервирован за ошибками.
LIST_END_TEXT = 'Это все подходящие фильмы из подборки.'

# --- Карточка фильма (фаза 2, A4, add-compact-buttons-and-movie-card) ---
# Лимит длины текста сообщения с постером (caption) по Bot API.
MOVIE_CARD_CAPTION_LIMIT = 1024
# Кнопки карточки: ссылка на Кинопоиск (url), похожие (similar-интент по
# id фильма) и возврат к списку (с A5 доставляется редактированием
# сообщения — изменение add-blockquote-card-and-edit-callbacks).
CARD_LINK_BUTTON_TEXT = '🔗 Кинопоиск'
CARD_SIMILAR_BUTTON_TEXT = '🎬 Похожие'
CARD_BACK_BUTTON_TEXT = '⬅️ К списку'
CARD_BACK_CALLBACK = 'back:list'
# По две кнопки в ряду: три подписи с эмодзи в одном ряду Telegram ужимает
# с обрезкой текста на узких экранах.
CARD_BUTTONS_PER_ROW = 2

# --- Прогрессивное раскрытие карточки (фаза 2, A7, add-blockquote-card-and-edit-callbacks) ---
# Bot API ≥ 7.3 (в проекте PTB 22.5): <blockquote expandable> — сворачиваемая
# цитата. Описание фильма разворачивается тапом: первая строка карточки —
# «вердикт» (название, год, жанр, рейтинг, 🍅-бейдж), длинный синопсис не
# «простынюет» чат (docs/research/research_uiux_telegram.md, задача A7).
CARD_QUOTE_OPEN = '<blockquote expandable>'
CARD_QUOTE_CLOSE = '</blockquote>'
# Разделитель вердикта и цитаты — перевод строки: вердикт остаётся первой
# строкой карточки, цитата идёт отдельным блоком под ним.
CARD_QUOTE_SEPARATOR = '\n'
# Полная цена обёртки (23 + 1 + 13 = 37 символов): вычитается из бюджета
# caption ДО расчёта места под вердикт и описание, поэтому закрывающий тег
# присутствует всегда и блокquote не вылезает за лимит (design.md D3).
CARD_QUOTE_OVERHEAD = len(CARD_QUOTE_OPEN) + len(CARD_QUOTE_SEPARATOR) + len(CARD_QUOTE_CLOSE)
# Обёртка спойлера Telegram (HTML: <span class="tg-spoiler">) — ТОЧКА
# РАСШИРЕНИЯ, а не текущее поведение карточки: описание Кинопоиска неделимо,
# спойлеры не выдумываются эвристикой (см. wrap_spoiler, design.md D5).
CARD_SPOILER_OPEN = '<span class="tg-spoiler">'
CARD_SPOILER_CLOSE = '</span>'

# --- Случайный фильм вечера (фаза 3, B1, add-onboarding-and-error-recovery) ---
# Кнопка «🎲 Случайный фильм вечера»: повышенный порог рейтинга гарантирует
# качество мгновенной рекомендации (первое впечатление о боте), а размер
# кандидатного списка — случайность выбора при повторных нажатиях
# (показанный фильм исключается по session.last_movies).
RANDOM_MIN_RATING = 7.5
RANDOM_CANDIDATE_LIMIT = 20

# --- Watchlist «📌 Мой список» (фаза 3, B4, add-watchlist) ---
# Кнопка сохранения в карточке фильма (A4): всегда «📌 Сохранить» —
# состояние «уже сохранён» обрабатывается ОТВЕТОМ маршрута save: («Уже в
# списке»), а не подменой подписи: рендер карточки не ходит в БД (design.md D3).
CARD_SAVE_BUTTON_TEXT = '📌 Сохранить'
# Префиксы callback-маршрутов watchlist (диспетчер бота маршрутизирует по
# префиксу): save:{kinopoisk_id} — сохранить, unsave:{kinopoisk_id} —
# удалить, wpage:{offset} — страница списка, watchlist:view — первая
# страница. Все callback_data короткие (≤64 байт): «unsave:» + до 10 цифр.
SAVE_CALLBACK_PREFIX = 'save:'
UNSAVE_CALLBACK_PREFIX = 'unsave:'
WATCHLIST_PAGE_PREFIX = 'wpage:'
WATCHLIST_CALLBACK = 'watchlist:view'
# Кнопка перехода к списку в ответе на сохранение («✅ Сохранено»/«Уже в
# списке») — единый литерал для бота и тестов.
WATCHLIST_OPEN_BUTTON_TEXT = '📌 Мой список'
# Подписи кнопок списка (эмодзи по docs/emoji_guideline.md: 🗑️ — удаление,
# ⬇️ — пагинация/«показать ещё», 🎲 — случайный фильм).
WATCHLIST_REMOVE_BUTTON_TEXT = '🗑️ Удалить'
WATCHLIST_MORE_BUTTON_TEXT = '⬇️ Ещё 5'
WATCHLIST_RANDOM_BUTTON_TEXT = '🎲 Случайный фильм'
# Размер страницы списка (паттерн B3) и заголовок сообщения списка.
WATCHLIST_PAGE_LIMIT = 5
WATCHLIST_HEADER = '📌 Мой список:'
# Заглушка пустого списка (B7: без dead-end — объяснение + мгновенное
# действие «🎲 Случайный фильм» существующего маршрута random:movie).
WATCHLIST_EMPTY_TEXT = '📌 Список пуст — добавьте фильмы кнопкой «📌 Сохранить» в карточках.'


def truncate_button_text(text: Optional[str], limit: int = BUTTON_TEXT_LIMIT) -> str:
    """Обрезать подпись inline-кнопки до лимита Telegram (A3).

    Возвращает текст не длиннее `limit` символов: превышение усекается с
    завершающим «…». Пустое значение (`None`, пустая строка, строка из
    пробелов) заменяется прочерком «—» — пустая подпись кнопки недопустима,
    а «Подробнее: None» (nit №1 ревью A2) становится невозможным.

    Лимит Bot API задан в СИМВОЛАХ, поэтому достаточно `len()` по
    code point'ам Python: эмодзи-цифры кнопок-номеров (`1️⃣` = 3 code
    point) проходят как есть, переход на UTF-16 code units не требуется.
    Обрезка не гарантирует сохранность эмодзи-последовательностей (ZWJ) —
    для названий фильмов это допустимо и лучше отказа отправки.
    """
    value = str(text or '').strip()
    if not value:
        return BUTTON_TEXT_EMPTY
    if len(value) <= limit:
        return value
    if limit <= 1:
        # Вырожденный лимит — места на «…» нет, режем жёстко
        return value[:max(limit, 0)] or BUTTON_TEXT_EMPTY
    return value[:limit - 1].rstrip() + '…'


def number_button_label(index: int) -> str:
    """Подпись кнопки-номера: эмодзи-цифра, а для номера > 10 — число."""
    if 1 <= index <= len(NUMBER_BUTTON_LABELS):
        return NUMBER_BUTTON_LABELS[index - 1]
    return str(index)


def compute_list_hash(movies: List[Dict[str, Any]]) -> str:
    """Короткий отпечаток выдачи (B3): blake2b по списку id фильмов.

    Детерминирован: зависит только от состава и порядка списка — один и
    тот же список даёт одинаковый hash и при рендере, и при разборе
    callback'а `page:{offset}:{hash}`. В сессии НЕ хранится (схема БД не
    меняется): отпечаток всегда выводится из `session.last_movies`,
    поэтому рассинхрон «поле vs список» невозможен конструктивно. Назначение
    — защита устаревших кнопок: выдача перезаписана новым подбором → hash
    расходится → callback обрабатывается дружелюбно (B7), а не показом
    чужого среза (design.md D2).
    """
    payload = ','.join(str(m.get('id') or '') for m in movies).encode('utf-8')
    return hashlib.blake2b(payload, digest_size=LIST_HASH_BYTES).hexdigest()


def clamp_page_offset(total: int, offset: int, limit: int = LIST_DISPLAY_LIMIT) -> int:
    """Валидный offset страницы пагинации (B3): снап на границу, вне — 0.

    Защитная нормализация: offset из callback'а всегда кратен размеру
    страницы (кнопки генерируются рендерером), но «рукодельный» или
    устаревший callback не должен давать пустой срез или падение —
    отрицательные значения, выход за список и некратные offset
    нормализуются к детерминированному началу страницы.
    """
    if limit <= 0 or offset <= 0 or offset >= total:
        return 0
    return (offset // limit) * limit


def format_rt_badge(movie: Dict[str, Any]) -> str:
    """Собрать бейдж Tomatometer «🍅 91%» для словаря фильма.

    Пустая строка возвращается, если оценки критиков нет либо она
    некорректна: None/отсутствие ключа (нет IMDb ID, сбой OMDb, выключен
    feature flag Epic B), нечисловое значение, bool или выход за диапазон
    0–100. `rt_score == 0` — валидные данные («0% одобрения»), бейдж
    выводится. Текст бейджа не содержит HTML-символов; логировать его
    нельзя (консоль cp1251 падает на эмодзи — ловушка AGENTS.md).
    """
    rt_score = movie.get('rt_score')
    if isinstance(rt_score, bool) or not isinstance(rt_score, int):
        # None, строки, дробные и прочие мусорные значения — «нет данных»
        return ''
    if not RT_BADGE_MIN <= rt_score <= RT_BADGE_MAX:
        # Значение вне шкалы Tomatometer трактуем как отсутствие оценки
        return ''
    return f'{RT_BADGE_EMOJI} {rt_score}%'


def rt_badge_suffix(movie: Dict[str, Any]) -> str:
    """Суффикс « · 🍅 91%» для строки списка/карточки либо пустая строка.

    Разделитель возвращается вместе с бейджем, чтобы при отсутствии
    RT-данных в тексте не оставалось висящих пробелов и разделителей:
    выдача без `rt_score` побайтово совпадает с прежней.
    """
    badge = format_rt_badge(movie)
    return f'{RT_BADGE_SEPARATOR}{badge}' if badge else ''


def format_list_line(index: int, movie: Dict[str, Any]) -> str:
    """Строка нумерованного списка фильмов (A2, add-rich-movie-list-format).

    Формат:
    ``N. <b><a href="{url}">Название</a></b> (2021) · комедия, США · ⭐ 7.8 (IMDB)``
    плюс RT-бейдж « · 🍅 NN%» в конце строки при валидном rt_score.

    При отсутствии данных часть опускается ЦЕЛИКОМ — без висящих « · »
    и пустых скобок (тот же паттерн, что у rt_badge_suffix):
    - нет kinopoisk_url — название жирное, но без ссылки;
    - нет/пуст год — часть «(год)» не выводится;
    - жанр и страна образуют одну часть «жанр, страна»: выводится то
      из двух, что непусто; нет ни того, ни другого — части нет;
    - источник рейтинга в скобках — только если и rating, и
      rating_source валидны (не «нет данных»).

    Все пользовательские значения экранируются html.escape; в атрибуте
    href экранируются и кавычки (quote=True), чтобы значение URL не
    могло разорвать разметку.
    """
    title = html.escape(str(movie.get('title') or '—'))
    url = movie.get('kinopoisk_url')
    if url:
        title_part = f'<b><a href="{html.escape(str(url), quote=True)}">{title}</a></b>'
    else:
        title_part = f'<b>{title}</b>'
    # Год идёт вплотную к названию — «<b>Название</b> (2021)», без « · »
    head = f'{index}. {title_part}'
    year = movie.get('year')
    if year is not None and year != '':
        head += f' ({html.escape(str(year))})'
    parts = [head]

    # Жанр и страна — одна часть «жанр, страна» (сентинели «нет данных»:
    # None и пустая строка; движок отдаёт '' при пустом списке значений)
    meta_values = []
    for key in ('genre', 'country'):
        value = str(movie.get(key) or '').strip()
        if value:
            meta_values.append(value)
    if meta_values:
        parts.append(html.escape(', '.join(meta_values)))

    # Рейтинг выводится всегда: '—' при отсутствии данных (прежнее
    # поведение); источник — только при валидных rating И rating_source
    rating = movie.get('rating')
    if rating is None or rating == '':
        rating = '—'
    rating_part = f'⭐ {html.escape(str(rating))}'
    rating_source = str(movie.get('rating_source') or '').strip()
    if rating != '—' and rating_source and rating_source != '—':
        rating_part += f' ({html.escape(rating_source)})'
    parts.append(rating_part)

    return LIST_PART_SEPARATOR.join(parts) + rt_badge_suffix(movie)


def _format_card_verdict(movie: Dict[str, Any]) -> str:
    """Жирный «вердикт» карточки — первая строка без описания (A7).

    Состав: 🎬, название, год, жанр, рейтинг и RT-бейдж « · 🍅 NN%» —
    ЦЕЛИКОМ в одном `<strong>` (вложенные теги жирного Telegram не нужны).
    Эмодзи-маркер 🎬 остаётся ВНЕ тега: он не часть вердикта, а указатель
    «это карточка фильма» (гайдлайн «1 эмодзи = 1 смысл»). Формулировка
    строки СОХРАНЕНА из B7/A4 («{title} ({year}) — {genre} с рейтингом
    {rating}{rt_suffix}.»), чтобы регрессия RT-бейджа
    (tests/test_rt_badge_compat.py: «рейтингом 8.8», «🍅 91%») оставалась
    зелёной. Пропущенные поля нормализованы по образцу строки списка (A2,
    ревью m4): None/пустые название, жанр и рейтинг → «—», а часть
    «(год)» при None/пустом годе ОПУСКАЕТСЯ целиком — литералов «None»,
    пустых скобок и висящих разделителей в вердикте не бывает (данные
    приходят в том числе из `_handle_info_callback`, где year/rating —
    `raw.get(...)` с возможным None). Все значения экранируются
    html.escape — тот же контракт, что у format_list_line.
    """
    title = html.escape(str(movie.get('title') or '—'))
    year = movie.get('year')
    year_part = f' ({html.escape(str(year))})' if year is not None and year != '' else ''
    genre = html.escape(str(movie.get('genre') or '—'))
    rating = movie.get('rating')
    rating_text = '—' if rating is None or rating == '' else html.escape(str(rating))
    return f'🎬 <strong>{title}{year_part} — {genre} с рейтингом {rating_text}{rt_badge_suffix(movie)}.</strong>'


def format_movie_card(movie: Dict[str, Any]) -> str:
    """Общий HTML-текст карточки фильма (info-интент и кнопка «Подробнее»).

    Формат карточки один на два рендерера — ответ info-интента и карточку
    по callback: бейдж Tomatometer добавляется в единственной точке, чтобы
    текст не разъезжался между ними. Оба рендерера обращаются к функции
    через единую сборку `build_movie_card` (A4), которая дополнительно
    обеспечивает бюджет caption и inline-клавиатуру карточки.

    A7 (прогрессивное раскрытие): первая строка — жирный «вердикт»
    (`_format_card_verdict`), описание — в сворачиваемой цитате
    `<blockquote expandable>…</blockquote>` через перевод строки. Пустое
    описание (None/пустая строка/пробелы) — вердикт БЕЗ обёртки цитаты:
    пустой блокquote схлопнулся бы в визуальный артефакт. Описание
    нормализовано к `(movie.get('description') or '').strip()` (как в
    build_movie_card) — прежний `str(None)` рендерил литерал «None» и
    разъезжался со сборкой.
    """
    verdict = _format_card_verdict(movie)
    description = html.escape(str(movie.get('description') or '').strip())
    if not description:
        return verdict
    return f'{verdict}{CARD_QUOTE_SEPARATOR}{CARD_QUOTE_OPEN}{description}{CARD_QUOTE_CLOSE}'


def wrap_spoiler(text: Optional[str]) -> str:
    """Обернуть текст в спойлер Telegram `<span class="tg-spoiler">` (A7).

    ТОЧКА РАСШИРЕНИЯ, а не текущее поведение карточки: описание Кинопоиска
    неделимо, spoiler-части в данных нет, и спойлеры НЕ выдумываются
    эвристикой (требование A7). Хелпер подготовлен для будущего источника
    (например, LLM-выделение финала в B-эпике): экранирование тем же
    `html.escape`, что у полей карточки; пустое значение (None/пустая
    строка) возвращает пустую строку — пустой спойлер недопустим.
    """
    escaped = html.escape(str(text or ''))
    if not escaped:
        return ''
    return f'{CARD_SPOILER_OPEN}{escaped}{CARD_SPOILER_CLOSE}'


def _cut_html_safely(text: str, budget: int) -> str:
    """Обрезать HTML-текст до бюджета, не разрывая сущность или тег (A4).

    Описание карточки экранируется `html.escape`, поэтому в нём есть
    сущности вида «&amp;»: срез посреди сущности оставляет мусорный
    фрагмент «&am». Если граница среза попала внутрь незакрытой
    конструкции («&» без «;» либо «<» без «>» после него), срез
    откатывается к её началу. Проверяются обе конструкции — и тег, и
    сущность: тег в экранированном описании не встречается, но проверка
    защищает шапку карточки и будущие источники текста (A7).
    """
    if budget <= 0:
        return ''
    if len(text) <= budget:
        return text
    cut = text[:budget]
    for opener, closer in (('<', '>'), ('&', ';')):
        start = cut.rfind(opener)
        if start != -1 and closer not in cut[start:]:
            # Незакрытая конструкция — откатываемся к её началу
            cut = cut[:start]
    # Висящий пробел перед «…» не нужен
    return cut.rstrip()


def _cut_verdict_safely(text: str, limit: int) -> str:
    """Обрезать вердикт по безопасной границе, не оставляя открытый `<strong>` (A7).

    С A7 вердикт ЦЕЛИКОМ внутри одного `<strong>…</strong>`, и закрывающий
    тег стоит в самом конце: голый безопасный срез (`_cut_html_safely`)
    патологически длинной шапки оставил бы тег незакрытым и сломал parse.
    Поэтому при разбалансировке срез повторяется с запасом на длину
    закрывающего тега, и тег доклеивается явно. Итог гарантированно не
    длиннее `limit` и сбалансирован по `<strong>` (HTML-сущности и
    фрагменты тегов не разрываются — это обеспечивает `_cut_html_safely`).
    """
    closer = '</strong>'
    cut = _cut_html_safely(text, limit)
    if cut.count('<strong>') > cut.count(closer):
        shortened = _cut_html_safely(text, limit - len(closer))
        if '<strong>' in shortened:
            cut = shortened + closer
        else:
            # Предел настолько мал, что открывающий тег не помещается вместе
            # с закрывающим: возвращаем срез без тегов (баланс важнее жирного)
            cut = shortened
    return cut[:limit]


def _fit_card_head(movie: Dict[str, Any], limit: int) -> str:
    """Вердикт карточки (без описания), гарантированно не длиннее `limit`.

    Вырожденный случай — очень длинные поля, из-за которых вердикт не
    помещается в бюджет даже без описания. Укорачиваются ИМЕННО поля
    (сначала название, затем жанр), а вердикт пересобирается общим
    форматтером: так HTML-теги остаются сбалансированными (срез готовой
    строки разорвал бы `<strong>` — с A7 он закрывается в самом конце).
    Гарантию даёт посимвольность `html.escape` — экранирование префикса
    равно префиксу экранирования, поэтому снятие `excess + 1` символов
    поля убирает из вердикта не меньше `excess` символов. Если и это не
    помогло (патологически длинный рейтинг/год при коротких названии и
    жанре), вердикт обрезается `_cut_verdict_safely` — по безопасной
    границе и с явным закрытием `<strong>`: контракт «не длиннее limit и
    сбалансированная разметка» сохраняется при любых данных.
    """
    head = format_movie_card({**movie, 'description': ''})
    excess = len(head) - limit
    if excess <= 0:
        return head
    fitted = dict(movie)
    title = str(movie.get('title') or BUTTON_TEXT_EMPTY)
    keep = max(0, len(title) - excess - 1)
    fitted['title'] = f'{title[:keep]}…' if keep else BUTTON_TEXT_EMPTY
    head = format_movie_card({**fitted, 'description': ''})
    # A7: вердикт жирный ЦЕЛИКОМ, поэтому при перерасходе укорачивается и
    # жанр (вторая по «длине» часть строки) — рейтинг и RT-бейдж в хвосте
    # вердикта сохраняются, в отличие от голого среза
    excess = len(head) - limit
    if excess > 0:
        genre = str(fitted.get('genre') or BUTTON_TEXT_EMPTY)
        keep = max(0, len(genre) - excess - 1)
        fitted['genre'] = f'{genre[:keep]}…' if keep else BUTTON_TEXT_EMPTY
        head = format_movie_card({**fitted, 'description': ''})
    # Патологический остаток (длинный рейтинг/год и т.п.): обрезка по
    # безопасной границе с закрытием <strong> (A4-срез голым [:limit]
    # резал бы тег/сущность по живому — теперь это делает _cut_verdict_safely)
    if len(head) > limit:
        head = _cut_verdict_safely(head, limit)
    return head


def build_movie_card_keyboard(movie: Dict[str, Any]) -> InlineKeyboardMarkup:
    """Inline-клавиатура карточки фильма (A4).

    Состав: «🔗 Кинопоиск» — url-кнопка, добавляется ТОЛЬКО при валидной
    ссылке (непустая и начинается с `http`, та же проверка, что у постера);
    «🎬 Похожие» — callback `similar:{id}` существующего similar-интента;
    «📌 Сохранить» — callback `save:{id}` (watchlist B4: сохранение фильма
    в PostgreSQL, дубль даёт ответ «Уже в списке» без второй записи);
    «⬅️ К списку» — callback `back:list` (пересборка списка из сессии).
    Все `callback_data` короткие — запас до лимита Bot API (64 байта)
    многократный. При CARD_BUTTONS_PER_ROW=2 четыре кнопки дают два ровных
    ряда (с url-кнопкой — три), подписи с эмодзи не ужимаются Telegram.
    """
    movie_id = movie.get('id') or 0
    buttons: List[InlineKeyboardButton] = []
    url = str(movie.get('kinopoisk_url') or '').strip()
    if url.startswith('http'):
        buttons.append(InlineKeyboardButton(CARD_LINK_BUTTON_TEXT, url=url))
    buttons.append(InlineKeyboardButton(CARD_SIMILAR_BUTTON_TEXT, callback_data=f'similar:{movie_id}'))
    buttons.append(InlineKeyboardButton(CARD_SAVE_BUTTON_TEXT, callback_data=f'{SAVE_CALLBACK_PREFIX}{movie_id}'))
    buttons.append(InlineKeyboardButton(CARD_BACK_BUTTON_TEXT, callback_data=CARD_BACK_CALLBACK))
    rows = [buttons[i:i + CARD_BUTTONS_PER_ROW] for i in range(0, len(buttons), CARD_BUTTONS_PER_ROW)]
    return InlineKeyboardMarkup(rows)


def build_movie_card(movie: Dict[str, Any], limit: int = MOVIE_CARD_CAPTION_LIMIT) -> tuple[str, InlineKeyboardMarkup]:
    """Единая сборка карточки фильма: текст в бюджете + клавиатура (A4/A7).

    Единственная точка сборки для ОБОИХ рендереров карточки — info-интента
    (`_handle_info_request`) и callback `info:{id}`
    (`telegram_bot.handle_movie_detail`): текст не разъезжается между ними.
    Вердикт собирается существующим `format_movie_card` (единый источник
    формата, B7/A7), поэтому при описании, помещающемся в бюджет вместе с
    обёрткой цитаты, результат побайтово равен `format_movie_card(movie)` —
    обратная совместимость единой сборки.

    Бюджет caption (1024 символа) соблюдается за счёт описания и УЧИТЫВАЕТ
    обёртку прогрессивного раскрытия (A7, design.md D3):
    `CARD_QUOTE_OVERHEAD` (37 символов — открывающий тег, разделитель и
    закрывающий тег цитаты) вычитается из бюджета ДО расчёта места под
    вердикт и описание, поэтому блокquote не вылезает за лимит даже при
    патологической шапке. Порядок обрезки описания прежний: сначала
    экранирование, затем срез по безопасной границе (`_cut_html_safely`)
    и «…» — срез выполняется ВНУТРИ цитаты, закрывающий тег
    `</blockquote>` конкатенируется ПОСЛЕ и присутствует всегда. Вердикт
    (название, год, жанр, рейтинг, RT-бейдж) при обрезке сохраняется:
    бейдж живёт в вердикте и теряться не может. Если описание пустое,
    полностью съедено бюджетом вердикта либо от среза остался бы один
    маркер «…» (содержимого нет) — цитата не выводится вовсе (вердикт
    пересобирается под полный лимит): ни пустой блокquote, ни блокquote
    из одного «…» недопустимы.
    """
    keyboard = build_movie_card_keyboard(movie)
    description = html.escape(str(movie.get('description') or '').strip())
    if not description:
        # Описания нет — цитаты нет: вердикт получает полный бюджет
        return _fit_card_head(movie, limit), keyboard
    head = _fit_card_head(movie, limit - CARD_QUOTE_OVERHEAD)
    budget = limit - CARD_QUOTE_OVERHEAD - len(head)
    if len(description) > budget:
        # Один символ резервируется под «…»; если после безопасного среза
        # содержимого не осталось (бюджет ≤ 1 либо весь остаток съеден
        # откатом от разорванной сущности), описание НЕ выводится —
        # цитата из одного «…» бессодержательна (ревью m1)
        cut = _cut_html_safely(description, budget - 1) if budget > 1 else ''
        description = f'{cut}…' if cut else ''
    if not description:
        # Патологическая шапка съела бюджет целиком: пустая цитата недопустима —
        # вердикт пересобирается под полный лимит (без обёртки)
        return _fit_card_head(movie, limit), keyboard
    text = f'{head}{CARD_QUOTE_SEPARATOR}{CARD_QUOTE_OPEN}{description}{CARD_QUOTE_CLOSE}'
    # Инвариант: len(text) <= limit — обеспечен _fit_card_head (с запасом на
    # обёртку) и бюджетом описания. Защитный срез [:limit] здесь был бы хуже
    # выброса цитаты: он резал бы закрывающий тег и ломал баланс HTML
    # (ревью n1). Если инвариант всё же нарушен (формат вердикта изменили,
    # а бюджетную логику нет) — возвращаем вердикт под полный лимит:
    # он сбалансирован и гарантированно не длиннее limit.
    if len(text) > limit:
        return _fit_card_head(movie, limit), keyboard
    return text, keyboard


def render_watchlist_page(
    items: List[Dict[str, Any]],
    offset: int,
    total: int,
    limit: int = WATCHLIST_PAGE_LIMIT,
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура страницы списка «📌 Мой список» (B4).

    Отдельная модульная функция (не метод DialogueManager): у watchlist нет
    связи с LLM-пайплайном и сессиями, источник данных — результат
    `WatchlistManager.list_page` (страница элементов + общее количество).

    Формат строки элемента: ``N. <b>Название</b> (год)`` — название
    экранируется `html.escape` (пользовательские данные из Кинопоиска не
    ломают HTML-разметку, A6), часть «(год)» при отсутствии года опускается
    ЦЕЛИКОМ (паттерн format_list_line: без пустых скобок). Нумерация
    продолжается с offset (вторая страница — 6–10, паттерн B3).

    Клавиатура: у каждого элемента кнопка «🗑️ Удалить» (callback
    `unsave:{kinopoisk_id}`), отдельным рядом под ним; при наличии
    неотображённых элементов (total > offset + len(items)) — ряд навигации
    с кнопкой «⬇️ Ещё 5» (callback `wpage:{offset+limit}`), на последней
    странице её нет. Пустой список — дружелюбная заглушка с мгновенным
    действием «🎲 Случайный фильм» (существующий маршрут random:movie) —
    без dead-end (B7). Все подписи проходят truncate_button_text, все
    `callback_data` короткие (≤64 байт).
    """
    if not items:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(
            truncate_button_text(WATCHLIST_RANDOM_BUTTON_TEXT),
            callback_data=RANDOM_MOVIE_CALLBACK,
        )]])
        return WATCHLIST_EMPTY_TEXT, markup

    lines = [f'<strong>{html.escape(WATCHLIST_HEADER)}</strong>']
    rows: List[List[InlineKeyboardButton]] = []
    for i, item in enumerate(items, offset + 1):
        title = html.escape(str(item.get('title') or BUTTON_TEXT_EMPTY))
        year = item.get('year')
        year_part = f' ({year})' if year else ''
        lines.append(f'{i}. <b>{title}</b>{year_part}')
        kinopoisk_id = item.get('kinopoisk_id') or 0
        rows.append([InlineKeyboardButton(
            truncate_button_text(WATCHLIST_REMOVE_BUTTON_TEXT),
            callback_data=f'{UNSAVE_CALLBACK_PREFIX}{kinopoisk_id}',
        )])
    has_more = total > offset + len(items)
    if has_more:
        rows.append([InlineKeyboardButton(
            truncate_button_text(WATCHLIST_MORE_BUTTON_TEXT),
            callback_data=f'{WATCHLIST_PAGE_PREFIX}{offset + limit}',
        )])
    response = '\n'.join(lines) + '\n'
    return response, InlineKeyboardMarkup(rows)


class DialogueManager:
    def __init__(self, session_manager: SessionManager):
        self.session_manager = session_manager
        self.movie_agent = MovieAgent(use_api=True)
        self.llm_router = LLMRouter()
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.prompts_dir = os.path.join(current_dir, 'prompts')
        logger.info(f"Prompts directory: {self.prompts_dir}")
        if not os.path.exists(self.prompts_dir):
            os.makedirs(self.prompts_dir, exist_ok=True)
        self.intent_classifier = IntentClassifier(self.llm_router, self.prompts_dir)

        self.mood_to_genre = {
            'грустн': ['комедия', 'мультфильм', 'мюзикл', 'романтическая комедия'],
            'весел': ['комедия', 'приключения', 'фэнтези', 'семейный'],
            'устал': ['мелодрама', 'драма', 'семейный', 'приключения', 'исторический'],
            'скучно': ['боевик', 'триллер', 'приключения', 'фантастика', 'детектив'],
            'страшн': ['ужасы', 'триллер', 'мистика', 'психологический триллер'],
            'романт': ['мелодрама', 'романтическая комедия', 'драма'],
            'адреналин': ['боевик', 'триллер', 'приключения', 'военный', 'фантастика'],
            'умный': ['драма', 'исторический', 'психологический']
        }

    def _load_prompt(self, filename: str) -> str:
        path = os.path.join(self.prompts_dir, filename)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception as e:
            logger.error(f"Ошибка загрузки промпта {filename}: {e}")
            return ""

    async def process_message(self, http_session, user_id: str, message: str) -> Dict[str, Any]:
        try:
            message = sanitize_message(message)
            block_reason = precheck_message(message)
            if block_reason is not None:
                log_blocked(user_id, block_reason, message)
                return {
                    "response": get_refusal_text(block_reason),
                    "needs_clarification": False
                }

            # Синхронные обращения к БД уводим из event loop в поток
            session = await asyncio.to_thread(self.session_manager.get_session, user_id)
            intent_params = await self.intent_classifier.classify_with_llm(
                http_session,
                message,
                {'last_movies': session.last_movies, 'last_params': session.last_params}
            )
            intent = intent_params.get("intent", "initial")
            logger.info(f"Обработка запроса: intent={intent}, user_id={user_id}")

            if intent == OFFTOPIC_INTENT:
                log_blocked(user_id, "llm_offtopic", message)
                return {
                    "response": get_refusal_text("offtopic"),
                    "needs_clarification": False
                }
            elif intent == "info":
                result = await self._handle_info_request(http_session, message, intent_params, session)
            elif intent == "similar":
                result = await self._handle_similar_request(http_session, message, intent_params, session)
            elif intent == "alternative":
                result = await self._handle_refine_request(http_session, message, intent_params, session)
            else:
                result = await self._handle_general_request(http_session, message, intent_params, session)

            self._update_session(session, result)
            await asyncio.to_thread(self.session_manager.save_session, session)
            return result
        except Exception as e:
            logger.error(f"Ошибка обработки сообщения: {e}", exc_info=True)
            return {
                "response": "Извините, произошла ошибка при обработке вашего запроса. Попробуйте еще раз.",
                "needs_clarification": True
            }

    async def _handle_info_request(self, http_session, message: str, params: Dict, session: UserSession) -> Dict[
        str, Any]:
        target_movie = params.get("target_movie")
        if not target_movie:
            return {
                "response": "О каком фильме вы хотите узнать? Напишите название фильма.",
                "needs_clarification": True
            }
        found_movies = await self.movie_agent.search_by_title(http_session, target_movie)
        if not found_movies:
            return {
                "response": f"Не удалось найти информацию о фильме «{target_movie}». Проверьте название и попробуйте еще раз.",
                "needs_clarification": True
            }
        movie = found_movies[0]
        # Карточка собирается ЕДИНОЙ функцией (A4): тот же текст и та же
        # inline-клавиатура, что у карточки по callback (номер в списке),
        # поэтому текст не разъезжается между двумя рендерерами
        card_text, card_markup = build_movie_card(movie)
        return {
            "response": card_text,
            "reply_markup": card_markup,
            "movie": movie,
            "movies_list": [movie],
            "parameters": params,
            "needs_clarification": False
        }

    async def _handle_similar_request(self, http_session, message: str, params: Dict, session: UserSession, base_movie: Optional[Dict] = None) -> Dict[
        str, Any]:
        # base_movie — фильм, выбранный кнопкой «🎬 Похожие» в карточке (A4):
        # подбор идёт от НЕГО (жанр/год/рейтинг/тип), а не от всей последней
        # выдачи. Без параметра поведение прежнее — база берётся из сессии.
        # База подбора: фильм, выбранный кнопкой «🎬 Похожие» в карточке (A4),
        # либо — как прежде — последняя выдача из сессии
        base_movies: List[Dict] = [base_movie] if base_movie is not None else session.last_movies
        if not base_movies:
            return {
                "response": "У меня нет информации о предыдущих рекомендациях. Сначала найдите фильм, а потом попросите похожие.",
                "needs_clarification": True
            }
        last_movies = base_movies

        # Определяем movie_type
        if last_movies and isinstance(last_movies[0], dict):
            detected_type = last_movies[0].get('type') or last_movies[0].get('movie_type')
            movie_type = 'tv-series' if detected_type == 'tv-series' else 'movie'
        else:
            movie_type = session.last_params.get('movie_type', 'movie')

        # 🔑 1. Извлекаем страну и год/диапазон из НОВОГО запроса (params), иначе — из сессии
        country = params.get('country') or session.last_params.get('country')
        explicit_year = params.get('year')
        explicit_year_range = params.get('year_range')

        # 🔑 2. Жанр берём из последнего фильма (или доминирующий), если не указан явно
        genre = None
        if len(last_movies) == 1:
            base = last_movies[0]
            genre = base.get('genre', '').split(',')[0].strip() if base.get('genre') else None
        else:
            genres = []
            for m in last_movies:
                if m.get('genre'):
                    genres.extend([g.strip() for g in m['genre'].split(',') if g.strip()])
            if genres:
                from collections import Counter
                genre = Counter(genres).most_common(1)[0][0]

        # 🔑 3. Определяем диапазон лет
        year_range = None
        min_rating = 6.5

        if explicit_year_range:
            # Явный диапазон: "2000-2010"
            year_range = explicit_year_range
        elif explicit_year is not None:
            # Десятилетие: "2000" → (2000, 2009)
            if explicit_year % 10 == 0 and 1900 <= explicit_year <= 2020:
                year_range = (explicit_year, min(explicit_year + 9, CURRENT_YEAR))
            else:
                # Конкретный год: "2005" → (2002, 2008)
                year_range = (max(1900, explicit_year - 3), min(CURRENT_YEAR, explicit_year + 3))
        else:
            # Нет явного года — берём из последнего фильма
            if len(last_movies) == 1:
                year = last_movies[0].get('year')
                rating = last_movies[0].get('rating_imdb') or last_movies[0].get('rating_kp') or 6.5
                min_rating = max(6.0, rating - 0.5)
                if year:
                    year_range = (max(1900, year - 3), min(CURRENT_YEAR, year + 3))
            else:
                # Явные аннотации List[Any]: вывод типа из `dict.get()` даёт
                # «Any | None», на котором min/max/sorted ломают mypy. Проявилось
                # после добавления ветки base_movie (A4) — поведение не меняется.
                years: List[Any] = [m.get('year') for m in last_movies if m.get('year')]
                if years:
                    year_range = (max(1900, min(years) - 2), min(CURRENT_YEAR, max(years) + 2))
                ratings: List[Any] = [m.get('rating_imdb') or m.get('rating_kp') for m in last_movies]
                ratings = [r for r in ratings if r is not None]
                if ratings:
                    min_rating = max(6.0, sorted(ratings)[len(ratings) // 2] - 0.5)

        # 🔑 4. Выполняем поиск с учётом всех параметров
        movies = await self.movie_agent.recommend_movies(
            http_session,
            genre_name=genre,
            year_range=year_range,
            min_imdb_rating=min_rating,
            limit=25,
            movie_type=movie_type,
            country=country,  # ← страна тоже из нового запроса или сессии
            user_id=session.user_id,
            # Режим критиков — только из ТЕКУЩЕГО запроса, без наследования
            # из сессии: «похожие» после обычного подбора должны оставаться
            # обычным подбором (запросы без критиков не затрагиваются)
            critics_approved=bool(params.get('critics_approved'))
        )

        seen_ids = {m.get('id') for m in last_movies if m.get('id')}
        new_movies = [m for m in movies if m.get('id') not in seen_ids]
        final_movies = (new_movies[:13] or movies[:13])

        response_text, reply_markup = self._generate_list_response(
            final_movies,
            "Вот что ещё может вам понравиться:"
        )
        return {
            "response": response_text,
            "reply_markup": reply_markup,
            "movies_list": final_movies,
            "parameters": {"movie_type": movie_type, "country": country},
            "needs_clarification": False
        }

    async def _handle_refine_request(self, http_session, message: str, params: Dict, session: UserSession) -> Dict[
        str, Any]:
        if not session.last_params:
            return {
                "response": "Сначала задайте критерии поиска, например: «комедии 2020-х»",
                "needs_clarification": True
            }

        last_params = session.last_params.copy()
        last_movie_ids = {m['id'] for m in session.last_movies if m.get('id')}
        movie_type = last_params.get('movie_type', 'movie')
        mood_genres = last_params.get('mood_genres') or [last_params.get('genre')] if last_params.get('genre') else [
            'комедия']

        all_new_movies: List[Dict] = []
        seen_ids = set(last_movie_ids)
        for genre in mood_genres:
            if len(all_new_movies) >= 13:
                break
            raw_movies = await self.movie_agent.recommend_movies(
                http_session,
                genre_name=genre,
                year=last_params.get('year'),
                year_range=last_params.get('year_range'),
                actor=last_params.get('actor'),
                director=last_params.get('director'),
                country=last_params.get('country'),
                min_imdb_rating=last_params.get('min_rating') or 6.0,
                limit=30,
                movie_type=movie_type,
                user_id=session.user_id,
                # Режим «одобрено критиками» сохраняется при уточнении —
                # «другие варианты» продолжают прежний критический подбор;
                # свежее упоминание критиков в refine-запросе тоже учитывается
                critics_approved=bool(last_params.get('critics_approved') or params.get('critics_approved'))
            )
            for m in raw_movies:
                mid = m.get('id')
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    all_new_movies.append(m)
                    if len(all_new_movies) >= 13:
                        break

        if not all_new_movies:
            raw_movies = await self.movie_agent.recommend_movies(
                http_session,
                genre_name=last_params.get('genre'),
                year=last_params.get('year'),
                year_range=last_params.get('year_range'),
                actor=last_params.get('actor'),
                director=last_params.get('director'),
                country=last_params.get('country'),
                min_imdb_rating=last_params.get('min_rating') or 6.0,
                limit=13,
                movie_type=movie_type,
                user_id=session.user_id,
                critics_approved=bool(last_params.get('critics_approved') or params.get('critics_approved'))
            )
            all_new_movies = raw_movies[:13]
            response_text, reply_markup = self._generate_list_response(
                all_new_movies,
                "Повторяю предыдущие рекомендации:"
            )
        else:
            response_text, reply_markup = self._generate_list_response(
                all_new_movies,
                "Вот другие варианты:"
            )

        return {
            "response": response_text,
            "reply_markup": reply_markup,
            "movies_list": all_new_movies,
            "parameters": {**last_params, "movie_type": movie_type},
            "needs_clarification": False
        }

    async def _handle_general_request(self, http_session, message: str, params: Dict, session: UserSession) -> Dict[
        str, Any]:
        reply_markup = None
        message_lower = message.lower()
        logger.info(f"Обработка общего запроса: '{message}', params: {params}")
        # === Распознавание настроения ===
        mood_genres = []
        mood_triggers = {
            'грустн': ['грустн', 'плохое настроение', 'поднять настроение', 'грущу', 'грусть', 'хочу радости',
                       'подавлен', 'депресс', 'тоска', 'печал', 'уныл'],
            'весел': ['весел', 'смех', 'смешн', 'посмеяться', 'радост', 'хорошее настроение', 'радость',
                      'настроение отличное', 'счастлив', 'улыбк', 'забавн', 'юмор'],
            'устал': ['устал', 'выгор', 'энергии нет', 'отдохнуть', 'расслабиться', 'спокойн', 'тихий вечер',
                      'ничего напряжённого', 'без экшена', 'лёгкий фильм'],
            'скучно': ['скучно', 'нечего смотреть', 'занять себя', 'развлечься', 'что-то интересное', 'надоело всё',
                       'ищу что-то новое'],
            'страшн': ['страшн', 'испуг', 'боюсь', 'ужас', 'мистик', 'триллер', 'пуга', 'жутк', 'напряг', 'напряжённый',
                       'напрячь нервы', 'щекотка для нервов'],
            'романт': ['романт', 'влюблен', 'любовь', 'пара', 'вдвоем', 'нежн', 'сердечко', 'романтический вечер',
                       'чувств', 'влюблённость'],
            'адреналин': ['адреналин', 'экшн', 'боевик', 'напряжение', 'динамик', 'крутой', 'взрывы', 'гонки', 'погони',
                          'герои', 'спасение мира'],
            'умный': ['умный', 'глубок', 'философ', 'мысл', 'интеллектуальн', 'осмысл', 'не для всех', 'сложный',
                      'мозг', 'рефлексия', 'медитативн']
        }
        for mood_key, phrases in mood_triggers.items():
            if any(phrase in message_lower for phrase in phrases):
                mood_genres = self.mood_to_genre.get(mood_key, ['комедия'])
                logger.info(f"Определены жанры по настроению '{mood_key}': {mood_genres}")
                break

        explicit_genre = params.get('genre')
        if explicit_genre:
            mood_genres = [explicit_genre]

        use_query = params.get('actor') or None
        if not mood_genres and not explicit_genre and len(message.split()) <= 5:
            use_query = message

        year = params.get('year')
        year_range = params.get('year_range')
        is_decade = (year is not None and year % 10 == 0 and 1900 <= year <= 2020)
        current_year = CURRENT_YEAR
        if year is not None and is_decade and year_range is None:
            year_range = (year, min(year + 9, current_year))

        min_rating = params.get('min_rating') or (6.5 if is_decade else 6.0)
        limit = params.get('count') or 13  # ← Изменено: убрана привязка к is_decade
        movie_type = params.get('movie_type', 'movie')
        content_type = "сериалы" if movie_type == 'tv-series' else "фильмы"
        # === Множественный поиск по жанрам ===
        all_movies: List[Dict] = []
        seen_ids = set()
        if mood_genres:
            for genre in mood_genres:
                if len(all_movies) >= limit:
                    break
                movies = await self.movie_agent.recommend_movies(
                    http_session,
                    genre_name=genre,
                    year=year if not is_decade else None,
                    year_range=year_range,
                    actor=params.get('actor'),
                    director=params.get('director'),
                    country=params.get('country'),
                    min_imdb_rating=min_rating,
                    limit=limit,
                    movie_type=movie_type,
                    query=use_query,
                    user_id=session.user_id,
                    critics_approved=bool(params.get('critics_approved'))
                )
                for m in movies:
                    mid = m.get('id')
                    if mid and mid not in seen_ids:
                        seen_ids.add(mid)
                        all_movies.append(m)
                        if len(all_movies) >= limit:
                            break
        else:
            movies = await self.movie_agent.recommend_movies(
                http_session,
                genre_name=None,
                year=year if not is_decade else None,
                year_range=year_range,
                actor=params.get('actor'),
                director=params.get('director'),
                country=params.get('country'),
                min_imdb_rating=min_rating,
                limit=limit,
                movie_type=movie_type,
                query=use_query,
                user_id=session.user_id,
                critics_approved=bool(params.get('critics_approved'))
            )
            for m in movies:
                mid = m.get('id')
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    all_movies.append(m)
                    if len(all_movies) >= limit:
                        break

        movies = all_movies[:limit]
        logger.info(f"Найдено {movie_type}: {len(movies)}")

        if not movies:
            content_type = "сериалы" if movie_type == 'tv-series' else "фильмы"
            error_parts = []
            if params.get('country'):
                error_parts.append(f"стране '{params['country']}'")
            if params.get('director'):
                error_parts.append(f"режиссёру '{params['director']}'")
            if year_range:
                error_parts.append(f"{year_range[0]}-{year_range[1]} годах")
            elif year:
                error_parts.append(f"{year} году")
            if mood_genres or explicit_genre:
                genre_display = mood_genres[0] if mood_genres else explicit_genre
                error_parts.append(f"жанре '{genre_display}'")
            error_message = f"К сожалению, не удалось найти подходящие {content_type}."
            if error_parts:
                error_message = f"К сожалению, не удалось найти качественные {content_type} по вашему запросу ({', '.join(error_parts)})."
            return {
                "response": error_message,
                "reply_markup": None,
                "needs_clarification": True,
                "parameters": {**params, "movie_type": movie_type}
            }

        if mood_genres:
            mood_text = self._get_mood_text(message_lower)
            response_text, reply_markup = self._generate_list_response(
                movies,
                f"Вот {content_type}, которые помогут {mood_text}:"
            )
        elif any(word in message_lower for word in ['топ', 'лучш', 'рейтинг']):
            header = self._generate_top_header(
                mood_genres[0] if mood_genres else None,
                year,
                params.get('country'),
                year_range,
                content_type
            )
            response_text, reply_markup = self._generate_list_response(movies, header)
        else:
            header = self._generate_search_header(
                mood_genres[0] if mood_genres else None,
                year,
                params.get('country'),
                year_range,
                content_type
            )
            response_text, reply_markup = self._generate_list_response(movies, header)

        return {
            "response": response_text,
            "reply_markup": reply_markup,
            "movies_list": movies,
            "parameters": {
                **params,
                "genre": mood_genres[0] if mood_genres else None,
                "mood_genres": mood_genres,
                "mood": bool(mood_genres),
                "movie_type": movie_type
            },
            "needs_clarification": False
        }

    # === Синхронные вспомогательные методы (не делают I/O) ===

    def _get_mood_text(self, message_lower: str) -> str:
        if any(w in message_lower for w in ['грустн', 'печал', 'тоска', 'уныл']):
            return "поднять настроение"
        elif any(w in message_lower for w in ['весел', 'смех', 'смешн', 'радост', 'юмор']):
            return "соответствовать вашему весёлому настроению"
        elif any(w in message_lower for w in ['устал', 'выгор', 'расслаб', 'спокойн']):
            return "помочь расслабиться и отдохнуть"
        elif any(w in message_lower for w in ['скучно', 'нечего смотреть', 'занять']):
            return "развлечь и удивить"
        elif any(w in message_lower for w in ['страшн', 'испуг', 'пуга', 'жутк', 'напряг']):
            return "пощекотать нервы"
        elif any(w in message_lower for w in ['романт', 'влюблен', 'любовь', 'нежн']):
            return "создать романтическое настроение"
        elif any(w in message_lower for w in ['адреналин', 'экшн', 'боевик', 'взрывы']):
            return "зарядить адреналином"
        elif any(w in message_lower for w in ['умный', 'глубок', 'философ', 'интеллектуальн']):
            return "заставить задуматься"
        else:
            return "подойти вашему настроению"

    def _generate_top_header(self, genre: Optional[str], year: Optional[int], country: Optional[str] = None,
                             year_range: Optional[tuple] = None, content_type: str = "фильмов") -> str:
        parts = []
        if country:
            parts.append(f"из {country}")
        if genre:
            parts.append(f"в жанре {genre}")
        if year_range:
            if year_range[0] // 10 == year_range[1] // 10 and year_range[1] - year_range[0] == 9:
                decade = year_range[0]
                parts.append(f"{decade}-х годов")
            else:
                parts.append(f"{year_range[0]}–{year_range[1]} годов")
        elif year:
            if year % 10 == 0:
                parts.append(f"{year}-х годов")
            else:
                parts.append(f"в {year} году")  # ← добавлено "в"
        header = f"Топ {content_type}"
        if parts:
            header += " " + " ".join(parts)
        return header + ":"

    def _generate_search_header(self, genre: Optional[str], year: Optional[int], country: Optional[str] = None,
                                year_range: Optional[tuple] = None, content_type: str = "фильмов") -> str:
        parts = []
        if country:
            parts.append(f"из {country}")
        if genre:
            parts.append(f"в жанре {genre}")
        if year_range:
            if year_range[0] // 10 == year_range[1] // 10 and year_range[1] - year_range[0] == 9:
                decade = year_range[0]
                parts.append(f"{decade}-х годов")
            else:
                parts.append(f"{year_range[0]}–{year_range[1]} годов")
        elif year:
            if year % 10 == 0:
                parts.append(f"{year}-х годов")
            else:
                parts.append(f"в {year} году")  # ← "в"
        header = f"Рекомендации {content_type}"
        if parts:
            header += " " + " ".join(parts)
        return header + ":"

    def _generate_list_response(
        self,
        movies: List[Dict],
        header: str,
        limit: int = LIST_DISPLAY_LIMIT,
        offset: int = 0,
    ) -> tuple[str, InlineKeyboardMarkup]:
        """Текст списка выдачи + компактная inline-клавиатура (A2/A3, B2/B3).

        Отображается не более `limit` фильмов начиная с позиции `offset`
        (страница пагинации, B3): лимит добычи данных (13/25/30) на выдачу
        в сессию не влияет — полный список сохраняется в movies_list /
        session.last_movies и листается кнопкой «⬇️ Ещё 5». При `offset=0`
        текст ПОБАЙТОВО равен прежнему (регрессии формата A2 зелёные).

        Клавиатура (A3/B2): кнопки-номера 1️⃣… по BUTTONS_PER_ROW в ряду,
        `callback_data` прежний — `info:{id}` соответствующего фильма;
        нумерация строк и подписей ПРОДОЛЖАЕТСЯ с offset (вторая страница —
        6️⃣…🔟, для номера >10 — десятичный fallback `number_button_label`).
        Под номерами — ряд навигации с контекстными quick replies (B2):
        «🔄 Другие» (alt:list), «⬇️ Ещё 5» (page:{offset+limit}:{hash} —
        только если в выдаче остались неотображённые фильмы) и
        «🎲 Случайный» (random:movie). При исчерпании сохранённой выдачи
        (offset > 0 и продолжения нет) кнопка «⬇️ Ещё 5» скрывается, а в
        текст добавляется строка LIST_END_TEXT — БЕЗ нового запроса к
        движку (design.md D4). Подписи кнопок не зависят от названия
        фильма — ни «None», ни превышения лимита Telegram (nit №1 ревью A2
        закрыт конструктивно).
        """
        total = len(movies)
        offset = clamp_page_offset(total, offset, limit)
        has_more = offset + limit < total
        lines = [f"<strong>{html.escape(str(header))}</strong>"]
        number_buttons: List[InlineKeyboardButton] = []
        for i, movie in enumerate(movies[offset:offset + limit], offset + 1):
            # Строка со ссылкой на Кинопоиск, жанром/страной и источником
            # рейтинга (A2); RT-бейдж (B7) — в конце строки, только при
            # валидном rt_score
            lines.append(format_list_line(i, movie))
            movie_id = movie.get('id') or 0
            number_buttons.append(InlineKeyboardButton(
                truncate_button_text(number_button_label(i)),
                callback_data=f"info:{movie_id}",
            ))
        if offset > 0 and not has_more:
            # Последняя страница пагинации: явно сообщаем, что выдача
            # исчерпана (B3, критерий «это все подходящие»)
            lines.append(LIST_END_TEXT)
        # Ряды номеров + отдельный ряд навигации под ними (B2)
        rows = [number_buttons[i:i + BUTTONS_PER_ROW] for i in range(0, len(number_buttons), BUTTONS_PER_ROW)]
        nav_row = [InlineKeyboardButton(
            truncate_button_text(LIST_NAV_BUTTON_TEXT),
            callback_data=LIST_NAV_CALLBACK,
        )]
        if has_more:
            nav_row.append(InlineKeyboardButton(
                truncate_button_text(LIST_MORE_BUTTON_TEXT),
                callback_data=f"{PAGE_CALLBACK_PREFIX}{offset + limit}:{compute_list_hash(movies)}",
            ))
        nav_row.append(InlineKeyboardButton(
            truncate_button_text(LIST_RANDOM_BUTTON_TEXT),
            callback_data=RANDOM_MOVIE_CALLBACK,
        ))
        rows.append(nav_row)
        # Строки списка и заголовок разделяются переводом строки, завершающий
        # перевод строки сохранён — формат текста A2 не меняется
        response = "\n".join(lines) + "\n"
        return response, InlineKeyboardMarkup(rows)

    def render_movie_list(
        self,
        movies: List[Dict],
        header: str,
        offset: int = 0,
    ) -> tuple[str, InlineKeyboardMarkup]:
        """Публичный рендер списка выдачи (A4; с A5 — пересборка для «⬅️ К списку»).

        Тонкий делегат `_generate_list_response`: callback-маршрутам бота
        нужны ТЕ ЖЕ текст и клавиатура, что и при обычной выдаче, без
        обращения к приватному методу. С A5 результат пересборки
        доставляется обратным редактированием сообщения карточки
        (`telegram_bot._edit_or_send`), а не новым сообщением. Параметр
        `offset` (B3) открывает страницу пагинации: «⬅️ К списку»
        возвращает пользователя на ТУ ЖЕ страницу выдачи (design.md D5).
        """
        return self._generate_list_response(movies, header, offset=offset)

    async def find_similar_by_id(self, http_session, user_id: str, movie_id: int) -> Dict[str, Any]:
        """Подобрать похожие для фильма из последней выдачи по его id (A4).

        Тонкая обёртка над существующим similar-пайплайном для кнопки
        «🎬 Похожие» в карточке: фильм ищется в `session.last_movies`,
        дальше работает прежний `_handle_similar_request` с `base_movie`.
        Новый пайплайн подбора НЕ создаётся, LLM-классификатор не
        вызывается — интент известен из callback'а, а маршрут обязан быть
        детерминированным.

        Если фильма в сессии нет (рестарт бота, истечение сессии),
        возвращается дружелюбный ответ с следующим шагом — без dead-end.
        """
        # Синхронное обращение к хранилищу сессий уводим из event loop
        session = await asyncio.to_thread(self.session_manager.get_session, user_id)
        movie = next((m for m in session.last_movies if str(m.get('id')) == str(movie_id)), None)
        if movie is None:
            return {
                "response": (
                    "Не нашёл этот фильм в истории — возможно, подбор был давно или бот перезапускался.\n"
                    "Напишите название фильма, и я подскажу похожие."
                ),
                "needs_clarification": True
            }
        title = str(movie.get('title') or BUTTON_TEXT_EMPTY)
        result = await self._handle_similar_request(
            http_session,
            f"посоветуй похожие на {title}",
            {},
            session,
            base_movie=movie
        )
        # Сессия обновляется как после обычного запроса: следующий шаг
        # диалога («ещё похожие», «другие варианты») работает от новой выдачи
        self._update_session(session, result)
        await asyncio.to_thread(self.session_manager.save_session, session)
        return result

    async def get_random_movie(self, http_session, user_id: str) -> Optional[Dict[str, Any]]:
        """Мгновенная карточка случайного фильма с высоким рейтингом (B1).

        Переиспользует существующий движок рекомендаций: ОДИН вызов
        `movie_agent.recommend_movies` без жанра/года (общая выдача,
        RT-обогащение уже внутри движка), LLM-классификатор не вызывается —
        интент известен из callback'а, маршрут детерминированный и быстрый
        (критерий B1: первый результат < 10 с). Из кандидатов случайно
        выбирается фильм, показанные ранее (id из `session.last_movies`)
        исключаются — повторный тап даёт другой фильм, пока кандидатов
        хотя бы два; если после исключения пусто, берётся полный список.

        Выбранный фильм сохраняется в сессию как `movies_list=[movie]`
        (как после info-интента), поэтому кнопки карточки «🎬 Похожие» и
        «⬅️ К списку» работают штатно. Возвращает dict той же формы, что
        `process_message`, либо `None`, если кандидатов нет (движок
        глотает сбои API и возвращает пустой список) — бот отвечает
        дружелюбной ошибкой класса «сеть/Kinopoisk» с кнопкой повтора.
        """
        session = await asyncio.to_thread(self.session_manager.get_session, user_id)
        movies = await self.movie_agent.recommend_movies(
            http_session,
            min_imdb_rating=RANDOM_MIN_RATING,
            limit=RANDOM_CANDIDATE_LIMIT,
            movie_type='movie',
            user_id=user_id,
        )
        if not movies:
            return None
        shown_ids = {str(m.get('id')) for m in (session.last_movies or []) if m.get('id')}
        fresh = [m for m in movies if str(m.get('id')) not in shown_ids]
        movie = random.choice(fresh or movies)
        # Карточка собирается ЕДИНОЙ функцией (A4): тот же формат, что у
        # карточки info-интента и callback-маршрута `info:`
        card_text, card_markup = build_movie_card(movie)
        result = {
            "response": card_text,
            "reply_markup": card_markup,
            "movie": movie,
            "movies_list": [movie],
            "needs_clarification": False,
        }
        self._update_session(session, result)
        await asyncio.to_thread(self.session_manager.save_session, session)
        return result

    def _update_session(self, session: UserSession, result: Dict):
        if "movies_list" in result:
            session.last_movies = result["movies_list"]
        if "parameters" in result:
            session.last_params = result["parameters"]
        session.update_activity()

    def clear_user_session(self, user_id: str):
        self.session_manager.clear_session(user_id)

