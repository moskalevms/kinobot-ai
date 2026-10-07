"""Guard-тесты T7: инварианты раскладки inline-клавиатур ВСЕХ билдеров.

Изменение `add-button-layout-guard-tests-t7` (бэклог
`backlog/backlog_2026-10-04_bugfix_telegram_ui.md`, задача T7). Закрепляет
решения раскладки T3 (длинные подписи — отдельный ряд или перенос `\\n`),
чтобы новый/изменённый билдер кнопок не мог молча вернуть баг 04.10.2026:
две длинные подписи в одном ряду Telegram делит пополам и обрезает
многоточием, хотя формальные лимиты Bot API не нарушены.

Инварианты (дельта-спека button-layout-guard):
1. подпись ≤64 символа (лимит Bot API);
2. `callback_data` непустая и ≤64 БАЙТ в UTF-8 (лимит Bot API байтовый,
   кириллица стоит 2 байта — паттерн tests/test_mood_keyboard_t5.py);
3. подпись идемпотентна относительно `truncate_button_text` (A3/T3):
   повторная обрезка не меняет текст — значит подпись либо уже прогнана
   через обрезку, либо в ней нет обрамляющих пробелов/пустоты;
4. эвристика ширины ряда: в ряду из ≥2 кнопок сумма видимых ширин подписей
   ≤ `ROW_LABEL_SUM_LIMIT` (~44 символа — ширина экрана из баг-репорта);
   ширина подписи считается по САМОЙ ДЛИННОЙ строке (`\\n` растит высоту
   кнопки, а не ширину — разрешённый T3 способ укладки); ряд из одной
   кнопки освобождён (раскладка T3 «ряд на кнопку»). Порог — константа
   теста, а не догма: ложные срабатывания гасятся документированным
   allowlist'ом `WIDTH_ALLOWLIST` (сейчас пуст).

Покрытие — ВСЕ функции-билдеры inline-клавиатур из `src/telegram_bot.py` и
`src/dialogue_manager.py` (реестр `BUILDER_COVERAGE`), включая ветвления
(классы ошибок и цели повтора, карточка с url-кнопками/трейлером/
провайдерами и без них, watchlist с продолжением и пустой, список выдачи
с «⬇️ Ещё 5» и последняя страница, пустая и непустая статистика). Полнота
реестра защищена AST-сканом исходников: новый билдер с конструктором
`InlineKeyboardMarkup(` обрушит `test_ast_scan_finds_no_uncovered_builders`
до добавления кейса в guard.

Негативный контроль (критерий приёмки T7): те же инвариант-функции на
синтетических данных ОБЯЗАНЫ обнаруживать искусственно длинную пару
кнопок в ряду, >64-байтный `callback_data`, подпись >64 символа и
необрезанную подпись; позитивный контроль — валидный ряд чист.

Все проверки офлайн: фейки и фабрики из `tests/conftest.py`, реальные
Telegram, PostgreSQL, LLM и сеть не используются.
"""
import ast
from pathlib import Path
from typing import AbstractSet, Any, Callable, Dict, List, Set, Tuple

import pytest
from telegram import InlineKeyboardButton

import dialogue_manager
import telegram_bot
from conftest import make_list_movies, make_manager, make_movie
from dialogue_manager import (
    BUTTON_TEXT_LIMIT,
    build_feedback_rating_keyboard,
    build_feedback_row,
    build_movie_card,
    build_movie_card_keyboard,
    render_watchlist_page,
    truncate_button_text,
)

ROOT = Path(__file__).resolve().parents[1]

# --- Настраиваемые пороги guard'а (риск T7: эвристика — предупреждение,
# а не догма; меняются в одном месте) ---

# Лимит Bot API для `callback_data` — БАЙТЫ, не символы (кириллица стоит
# 2 байта в UTF-8), поэтому проверяется `len(data.encode('utf-8'))`
CALLBACK_DATA_LIMIT_BYTES = 64
# Эвристика читаемости ряда: суммарная ширина подписей ряда из ≥2 кнопок.
# Та же константа, что в tests/test_genre_keyboard_t6.py (T6 заложил
# эвристику заранее, T7 закрепляет её по всем билдерам).
ROW_LABEL_SUM_LIMIT = 44

# Документированные исключения эвристики ширины ряда (ложные срабатывания).
# ПУСТ на текущем коде: все существующие ряды проходят с запасом. Каждый
# будущий элемент ОБЯЗАН сопровождаться русским комментарием с причиной.
WIDTH_ALLOWLIST: frozenset = frozenset()

# Менеджер диалога для рендеров списка выдачи (офлайн, паттерн T5:
# `make_manager()` на уровне модуля — конструкторы без сети).
_DM = make_manager()

# Модули-источники билдеров (реестр покрытия и AST-скан)
_MODULES = {'telegram_bot': telegram_bot, 'dialogue_manager': dialogue_manager}

# Делегирующие обёртки без собственного конструктора `InlineKeyboardMarkup(`:
# учтены в реестре покрытия, но AST-скан их не находит (клавиатуру строит
# делегат) — поэтому исключены из проверки «устаревших» имён реестра.
# `build_stats_response` в этом списке НЕ ЧИСЛИТСЯ: после B2
# (add-menu-hub-exit-buttons-b1b2) сводка `/stats` сама конструирует
# `InlineKeyboardMarkup` — ряд выхода в хаб «🏠 Меню» (`menu:main`), поэтому
# AST-скан находит её как обычный билдер (кейсы `stats_empty`/`stats_filled`).
_DELEGATE_BUILDERS = frozenset({
    'build_exit_keyboard', 'build_movie_card', 'build_feedback_row',
})


# === Инвариант-функции (чистые — переиспользуются негативным контролем) ===


def label_width(text: str) -> int:
    """Видимая ширина подписи: самая длинная строка (design.md D3).

    Перенос `\\n` увеличивает высоту inline-кнопки, а не ширину (решение T3),
    поэтому в эвристику ряда идёт максимум длин строк. Для однострочных
    подписей метрика равна `len(text)` — совместима с проверкой T6.
    """
    return max((len(line) for line in text.split('\n')), default=0)


def check_button(button: Any, *, label_limit: int = BUTTON_TEXT_LIMIT, callback_bytes_limit: int = CALLBACK_DATA_LIMIT_BYTES) -> List[str]:
    """Нарушения инвариантов ОДНОЙ кнопки (пустой список — кнопка чиста).

    Инварианты 1-3 докстринга модуля: лимит символов подписи, байтовый
    лимит непустой `callback_data`, идемпотентность `truncate_button_text`.
    url-кнопки (`callback_data is None`) от байтовой проверки освобождены —
    у них нет callback_data как поля.
    """
    violations: List[str] = []
    text = button.text or ''
    if len(text) > label_limit:
        violations.append(
            f'подпись длиннее лимита Bot API ({len(text)} > {label_limit} символов): {text!r}'
        )
    if truncate_button_text(text) != text:
        violations.append(
            f'подпись НЕ прогнана через truncate_button_text (обрезка меняет текст): {text!r}'
        )
    data = button.callback_data
    if data is not None:
        if not data:
            violations.append('пустая callback_data (кнопка никуда не ведёт)')
        size = len(data.encode('utf-8'))
        if size > callback_bytes_limit:
            violations.append(
                f'callback_data длиннее лимита Bot API ({size} > {callback_bytes_limit} байт UTF-8): {data!r}'
            )
    return violations


def check_row_width(row: List[Any], *, limit: int = ROW_LABEL_SUM_LIMIT, allowlist: AbstractSet[str] = WIDTH_ALLOWLIST) -> List[str]:
    """Нарушение эвристики ширины ряда (инвариант 4 докстринга модуля).

    Ряд из ОДНОЙ кнопки освобождён: кнопка на всю ширину не обрезается
    Telegram пополам (раскладка T3 «ряд на кнопку»). Подпись из
    документированного allowlist'а освобождает ряд целиком (ложное
    срабатывание эвристики — порог приблизительный).
    """
    if len(row) < 2:
        return []
    if any((button.text or '') in allowlist for button in row):
        return []
    total = sum(label_width(button.text or '') for button in row)
    if total > limit:
        labels = [button.text for button in row]
        return [
            f'ряд из {len(row)} кнопок слишком широкий: сумма ширин подписей '
            f'{total} > {limit} — нужен ряд из одной кнопки или перенос \\n: {labels!r}'
        ]
    return []


def collect_layout_violations(
    rows: List[List[Any]],
    *,
    label_limit: int = BUTTON_TEXT_LIMIT,
    callback_bytes_limit: int = CALLBACK_DATA_LIMIT_BYTES,
    row_width_limit: int = ROW_LABEL_SUM_LIMIT,
    allowlist: AbstractSet[str] = WIDTH_ALLOWLIST,
) -> List[str]:
    """Все нарушения раскладки клавиатуры (список рядов кнопок)."""
    violations: List[str] = []
    for row_index, row in enumerate(rows, 1):
        for button in row:
            for violation in check_button(button, label_limit=label_limit, callback_bytes_limit=callback_bytes_limit):
                violations.append(f'ряд {row_index}: {violation}')
        for violation in check_row_width(row, limit=row_width_limit, allowlist=allowlist):
            violations.append(f'ряд {row_index}: {violation}')
    return violations


# === Фикстуры клавиатур (ленивые фабрики — design.md D2) ===


def _movie_with_providers() -> Dict[str, Any]:
    """Фильм карточки со всеми url-кнопками: Кинопоиск, трейлер, 3 провайдера."""
    return make_movie(
        trailer_url='https://www.kinopoisk.ru/film/435/trailer/',
        watch_providers=[
            {'name': 'Okko', 'url': 'https://okko.tv/movie/435'},
            {'name': 'Кинопоиск HD', 'url': 'https://hd.kinopoisk.ru/film/435'},
            {'name': 'Иви', 'url': 'https://www.ivi.ru/watch/435'},
        ],
    )


def _movie_minimal() -> Dict[str, Any]:
    """Фильм карточки БЕЗ валидной ссылки: url-кнопок и трейлера нет."""
    return make_movie(kinopoisk_url='', poster_url=None)


def _watchlist_items(count: int, start: int = 1) -> List[Dict[str, Any]]:
    """Элементы страницы «📌 Мой список» (реалистичные короткие названия)."""
    return [
        {'kinopoisk_id': 400000 + start + i, 'title': f'Фильм {start + i}', 'year': 2020 + i % 5}
        for i in range(count)
    ]


def _stats_filled_rows() -> List[List[Any]]:
    """Статистика с данными: ряд выхода в хаб «🏠 Меню» (`menu:main`).

    До B2 (add-menu-hub-exit-buttons-b1b2) сводка была информационным
    сообщением без клавиатуры; теперь она ОБЯЗАНА содержать кнопку выхода в
    главное меню, поэтому кейс проверяется инвариантами наравне с прочими.
    """
    _text, markup = telegram_bot.build_stats_response(
        {'queries': 3, 'rated': 2, 'avg_rating': 7.4, 'watched': 1, 'nope': 0, 'watchlist': 1}
    )
    assert markup is not None, 'контракт B2: непустая сводка идёт с клавиатурой выхода в хаб'
    return markup.inline_keyboard


# Реестр кейсов guard'а: id → ленивая фабрика списка рядов клавиатуры.
# Построение выполняется ВНУТРИ теста, а не на этапе сбора (импорт
# `telegram_bot` тяжёлый — паттерн `install_callback_mocks` conftest).
KEYBOARD_CASES: List[Tuple[str, Callable[[], List[List[Any]]]]] = [
    # --- telegram_bot.py: онбординг, меню, топы, mood/genre (T5/T6) ---
    ('onboarding', lambda: telegram_bot.build_onboarding_keyboard().inline_keyboard),
    ('main_menu', lambda: telegram_bot.get_main_menu().inline_keyboard),
    ('top_menu', lambda: telegram_bot.get_top_menu().inline_keyboard),
    ('mood_t5', lambda: telegram_bot.build_mood_keyboard().inline_keyboard),
    ('genre_t6', lambda: telegram_bot.build_genre_menu().inline_keyboard),
    # --- telegram_bot.py: ошибки/выходы — ВСЕ ветки build_error_keyboard ---
    ('error_network', lambda: telegram_bot.build_error_keyboard(telegram_bot.ERROR_CLASS_NETWORK).inline_keyboard),
    ('error_network_retry', lambda: telegram_bot.build_error_keyboard(telegram_bot.ERROR_CLASS_NETWORK, 'info:447301').inline_keyboard),
    ('error_network_retry_random', lambda: telegram_bot.build_error_keyboard(telegram_bot.ERROR_CLASS_NETWORK, 'random').inline_keyboard),
    ('error_llm_retry', lambda: telegram_bot.build_error_keyboard(telegram_bot.ERROR_CLASS_LLM, 'mood:pick:устал').inline_keyboard),
    ('error_generic_retry', lambda: telegram_bot.build_error_keyboard(telegram_bot.ERROR_CLASS_GENERIC, 'info:447301').inline_keyboard),
    ('error_generic_retry_random', lambda: telegram_bot.build_error_keyboard(telegram_bot.ERROR_CLASS_GENERIC, 'random').inline_keyboard),
    ('exit', lambda: telegram_bot.build_exit_keyboard().inline_keyboard),
    ('unknown_callback', lambda: telegram_bot.build_unknown_callback_keyboard().inline_keyboard),
    # --- telegram_bot.py: watchlist/фидбек/статистика ---
    ('watchlist_open', lambda: telegram_bot.build_watchlist_open_keyboard().inline_keyboard),
    ('feedback_saved', lambda: telegram_bot.build_feedback_saved_keyboard(447301).inline_keyboard),
    ('stats_empty', lambda: telegram_bot.build_stats_response({})[1].inline_keyboard),
    ('stats_filled', _stats_filled_rows),
    # --- dialogue_manager.py: карточка фильма (все варианты состава) ---
    ('movie_card_full', lambda: build_movie_card_keyboard(_movie_with_providers()).inline_keyboard),
    ('movie_card_minimal', lambda: build_movie_card_keyboard(_movie_minimal()).inline_keyboard),
    ('movie_card_unified', lambda: build_movie_card(make_movie())[1].inline_keyboard),
    # --- dialogue_manager.py: фидбек-ряд и панель оценки ---
    ('feedback_row', lambda: [build_feedback_row(447301)]),
    ('feedback_rating', lambda: build_feedback_rating_keyboard(447301).inline_keyboard),
    # --- dialogue_manager.py: страницы watchlist ---
    ('watchlist_page_more', lambda: render_watchlist_page(_watchlist_items(5), offset=0, total=7)[1].inline_keyboard),
    ('watchlist_last_page', lambda: render_watchlist_page(_watchlist_items(2), offset=5, total=7)[1].inline_keyboard),
    ('watchlist_empty', lambda: render_watchlist_page([], 0, 0)[1].inline_keyboard),
    # --- dialogue_manager.py: список выдачи (через публичный render_movie_list) ---
    ('movie_list_more', lambda: _DM.render_movie_list(make_list_movies(7), 'Подборка')[1].inline_keyboard),
    ('movie_list_last', lambda: _DM.render_movie_list(make_list_movies(7), 'Подборка', offset=2)[1].inline_keyboard),
]

_CASE_IDS = [case_id for case_id, _ in KEYBOARD_CASES]

# Кейсы без клавиатуры — документированы. ПУСТО с B2
# (add-menu-hub-exit-buttons-b1b2): непустая сводка `/stats` получила ряд
# выхода в хаб «🏠 Меню» (`menu:main`), поэтому кейс `stats_filled`
# проверяется инвариантами наравне с остальными. Множество сохранено как
# механизм: будущий «безклавиатурный» билдер документируется здесь же.
_CASES_WITHOUT_KEYBOARD: frozenset = frozenset()

# Покрытие билдеров кейсами: квалифицированное имя функции-билдера →
# ids кейсов, которые её прогоняют. Делегирующие обёртки
# (`build_exit_keyboard`, `build_movie_card`, `render_movie_list`) учтены
# наравне с прямыми конструкторами клавиатур.
BUILDER_COVERAGE: Dict[str, Tuple[str, ...]] = {
    'telegram_bot.build_onboarding_keyboard': ('onboarding',),
    'telegram_bot.build_error_keyboard': (
        'error_network', 'error_network_retry', 'error_network_retry_random',
        'error_llm_retry', 'error_generic_retry', 'error_generic_retry_random',
    ),
    'telegram_bot.build_exit_keyboard': ('exit',),
    'telegram_bot.build_unknown_callback_keyboard': ('unknown_callback',),
    'telegram_bot.build_watchlist_open_keyboard': ('watchlist_open',),
    'telegram_bot.build_feedback_saved_keyboard': ('feedback_saved',),
    'telegram_bot.build_stats_response': ('stats_empty', 'stats_filled'),
    'telegram_bot.get_main_menu': ('main_menu',),
    'telegram_bot.get_top_menu': ('top_menu',),
    'telegram_bot.build_mood_keyboard': ('mood_t5',),
    'telegram_bot.build_genre_menu': ('genre_t6',),
    'dialogue_manager.build_movie_card_keyboard': ('movie_card_full', 'movie_card_minimal'),
    'dialogue_manager.build_movie_card': ('movie_card_unified',),
    'dialogue_manager.build_feedback_row': ('feedback_row',),
    'dialogue_manager.build_feedback_rating_keyboard': ('feedback_rating',),
    'dialogue_manager.render_watchlist_page': ('watchlist_page_more', 'watchlist_last_page', 'watchlist_empty'),
    # приватный рендер списка — через публичный делегат render_movie_list
    'dialogue_manager._generate_list_response': ('movie_list_more', 'movie_list_last'),
}


def _functions_building_markups(module_path: Path) -> Set[str]:
    """Имена функций/методов модуля, конструирующих `InlineKeyboardMarkup(`.

    AST-скан исходника (офлайн): источник правды полноты guard'а — новый
    билдер клавиатур появляется здесь раньше, чем в реестре кейсов.
    """
    # `utf-8-sig`: исходники src/ сохранены с BOM, голый utf-8 оставляет
    # U+FEFF в начале текста и ast.parse падает SyntaxError
    tree = ast.parse(module_path.read_text(encoding='utf-8-sig'))
    names: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == 'InlineKeyboardMarkup':
                names.add(node.name)
                break
    return names


# === 1. Основной guard: инварианты раскладки по всем билдерам ===


@pytest.mark.parametrize('case_id,build_rows', KEYBOARD_CASES, ids=_CASE_IDS)
def test_button_layout_invariants_hold_for_every_builder(case_id: str, build_rows: Callable[[], List[List[Any]]]):
    """Каждая клавиатура каждого билдера удовлетворяет инвариантам 1-4."""
    rows = build_rows()
    violations = collect_layout_violations(rows)
    assert not violations, f'нарушения раскладки в кейсе «{case_id}»:\n' + '\n'.join(violations)


@pytest.mark.parametrize('case_id,build_rows', KEYBOARD_CASES, ids=_CASE_IDS)
def test_case_actually_builds_buttons(case_id: str, build_rows: Callable[[], List[List[Any]]]):
    """Фабрика кейса не выродилась в пустую (иначе guard молча «зеленеет»)."""
    rows = build_rows()
    if case_id in _CASES_WITHOUT_KEYBOARD:
        assert rows == [], f'кейс «{case_id}» документирован как безклавиатурный'
        return
    assert rows, f'кейс «{case_id}» не построил ни одного ряда кнопок'
    assert all(row for row in rows), f'кейс «{case_id}» построил пустой ряд'


def test_guard_registry_covers_all_documented_builders():
    """Реестр покрытия ссылается только на существующие билдеры и кейсы."""
    case_ids = set(_CASE_IDS)
    for builder, cases in BUILDER_COVERAGE.items():
        module_name, function_name = builder.split('.', 1)
        module = _MODULES[module_name]
        # билдер может быть методом DialogueManager (приватный рендер списка)
        attribute = getattr(module, function_name, None)
        if attribute is None:
            attribute = getattr(dialogue_manager.DialogueManager, function_name, None)
        assert callable(attribute), f'билдер {builder} исчез из кода'
        missing = set(cases) - case_ids
        assert not missing, f'кейсы {sorted(missing)} билдера {builder} не зарегистрированы'


def test_ast_scan_finds_no_uncovered_builders():
    """AST-скан: в src/ нет билдера клавиатур вне guard'а (полнота охвата).

    Падение этого теста означает, что в `telegram_bot.py`/`dialogue_manager.py`
    появился НОВЫЙ конструктор `InlineKeyboardMarkup(` — добавьте его в
    `KEYBOARD_CASES` и `BUILDER_COVERAGE`, а не удаляйте проверку.
    """
    scanned = {
        'telegram_bot': _functions_building_markups(ROOT / 'src' / 'telegram_bot.py'),
        'dialogue_manager': _functions_building_markups(ROOT / 'src' / 'dialogue_manager.py'),
    }
    covered: Dict[str, Set[str]] = {'telegram_bot': set(), 'dialogue_manager': set()}
    for builder in BUILDER_COVERAGE:
        module_name, function_name = builder.split('.', 1)
        covered[module_name].add(function_name)
    for module_name, found in scanned.items():
        uncovered = found - covered[module_name]
        assert not uncovered, (
            f'в {module_name}.py найден билдер клавиатур без guard-кейса: {sorted(uncovered)}'
        )
        stale = covered[module_name] - found - _DELEGATE_BUILDERS
        assert not stale, f'реестр покрытия {module_name} ссылается на несуществующие билдеры: {sorted(stale)}'


# === 2. Негативный контроль: инвариант-функции ОБЯЗАНЫ ловить нарушения ===


def test_negative_control_wide_pair_in_row_is_detected():
    """Искусственно длинная пара кнопок в ряду обнаруживается (критерий T7)."""
    wide_row = [
        InlineKeyboardButton('а' * 30, callback_data='wide:left'),
        InlineKeyboardButton('б' * 30, callback_data='wide:right'),
    ]
    # сумма ширин 60 > 44 — эвристика обязана сработать
    assert check_row_width(wide_row), 'широкая пара кнопок НЕ обнаружена — guard деградировал'
    assert collect_layout_violations([wide_row]), 'агрегатор не пропустил нарушение в общий список'


def test_negative_control_oversized_callback_data_is_detected():
    """`callback_data` из 33 кириллических символов (66 байт UTF-8) ловится."""
    data = 'ж' * 33
    assert len(data.encode('utf-8')) == 66, 'фикстура негативного контроля должна превышать 64 байта'
    button = InlineKeyboardButton('Кнопка', callback_data=data)
    violations = check_button(button)
    assert any('байт' in violation for violation in violations), f'байтовый лимит не сработал: {violations}'


def test_negative_control_long_and_untrimmed_labels_are_detected():
    """Подпись >64 символа и подпись с обрамляющими пробелами — нарушения."""
    long_button = InlineKeyboardButton('ы' * (BUTTON_TEXT_LIMIT + 1), callback_data='long')
    long_violations = check_button(long_button)
    assert any('лимит' in violation for violation in long_violations), 'превышение 64 символов не обнаружено'
    assert any('truncate_button_text' in violation for violation in long_violations), 'необрезанная подпись не обнаружена'

    untrimmed_button = InlineKeyboardButton('  Пробелы по краям  ', callback_data='untrimmed')
    assert any('truncate_button_text' in violation for violation in check_button(untrimmed_button)), (
        'подпись вне truncate_button_text (обрамляющие пробелы) не обнаружена'
    )

    empty_data_button = InlineKeyboardButton('Пусто', callback_data='')
    assert any('пустая' in violation for violation in check_button(empty_data_button)), (
        'пустая callback_data не обнаружена'
    )


def test_positive_control_valid_rows_are_clean():
    """Валидные синтетические ряды нарушений не дают (нет ложных срабатываний)."""
    valid_pair = [
        InlineKeyboardButton('🎲 Случайный', callback_data='random:movie'),
        InlineKeyboardButton('🏆 Топ', callback_data='retry:top'),
    ]
    assert collect_layout_violations([valid_pair]) == []
    # одиночная кнопка с длинной (но ≤64) подписью — разрешённая раскладка T3
    single_long = [InlineKeyboardButton('ы' * BUTTON_TEXT_LIMIT, callback_data='single')]
    assert check_row_width(single_long) == []
    assert collect_layout_violations([single_long]) == []
    # пара многоточных подписей: ширина — по самой длинной строке (D3)
    multiline_pair = [
        InlineKeyboardButton('🎭 Подобрать\nпо настроению', callback_data='mood:start'),
        InlineKeyboardButton('🎲 Фильм вечера\nслучайный', callback_data='random:movie'),
    ]
    assert label_width('🎭 Подобрать\nпо настроению') == len('по настроению')
    assert check_row_width(multiline_pair) == []


def test_row_width_threshold_and_allowlist_are_configurable():
    """Порог ширины и allowlist настраиваются (риск T7: тест — не догма)."""
    boundary_row = [
        InlineKeyboardButton('а' * 22, callback_data='b:left'),
        InlineKeyboardButton('б' * 22, callback_data='b:right'),
    ]
    # сумма ровно 44 — граница порога включительно
    assert check_row_width(boundary_row, limit=ROW_LABEL_SUM_LIMIT) == []
    assert check_row_width(boundary_row, limit=43), 'порог не настраивается'
    # документированный allowlist освобождает ряд от эвристики
    wide_row = [
        InlineKeyboardButton('в' * 30, callback_data='w:left'),
        InlineKeyboardButton('г' * 30, callback_data='w:right'),
    ]
    assert check_row_width(wide_row), 'контроль: широкая пара должна ловиться'
    assert check_row_width(wide_row, allowlist={'в' * 30}) == []
