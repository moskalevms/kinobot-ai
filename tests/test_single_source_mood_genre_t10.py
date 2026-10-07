"""Тесты T10: единый источник подписей настроений и запросов жанров.

Изменение `add-single-source-mood-genre-constants-t10` (бэклог
`backlog/backlog_2026-10-04_bugfix_telegram_ui.md`, задача T10; зависит от
T5/T6 — `tests/test_mood_keyboard_t5.py`, `tests/test_genre_keyboard_t6.py`,
чьё покрытие ЗДЕСЬ дополняется инвариантами единого источника, образец —
инвариант словарей B4 в `tests/test_mood_audit_b4.py`).

Покрытие (design.md D8):
- a) ключи `MOOD_BUTTON_LABELS` == ключи `mood_triggers` == ключи
  `mood_to_genre` — рассинхрон подписей и детерминированных маршрутов
  невозможен без падения теста;
- b) ключи `GENRE_QUERIES` == `GENRE_MENU_NAMES` и каждое имя жанра входит
  в значения `mood_to_genre`;
- c) выводимость: каждый запрос == `GENRE_QUERY_TEMPLATE.format(genre=name)`
  — второй источник формулировки запрещён (риск 5 бэклога T6);
- d) AST-guard: в `src/telegram_bot.py` нет собственных литеральных
  dict/tuple-списков ключей настроений/имён жанров (критерий приёмки T10
  «билдеры клавиатур не содержат собственных списков»); имена приходят
  импортом из `dialogue_manager` — дополнительно проверяется ИДЕНТИЧНОСТЬ
  объектов (`is`), а не только равенство значений;
- негативный контроль guard'а (паттерн `tests/test_migrations_t13.py`):
  сканер на искусственном исходнике с литеральным дублем ОБЯЗАН его найти,
  а на исходнике с импортом — не найти (guard не «холостой» и без ложных
  срабатываний на легальном коде).

Все проверки офлайн: `make_manager()` из `tests/conftest.py` (моки
LLM/сессий), AST-скан читает исходник текстом — без сети, реального
Telegram и PostgreSQL.
"""
import ast
from pathlib import Path
from typing import List, Set

import pytest

import dialogue_manager
import telegram_bot
from conftest import make_manager
from dialogue_manager import (
    GENRE_MENU_NAMES,
    GENRE_QUERIES,
    GENRE_QUERY_TEMPLATE,
    MOOD_BUTTON_LABELS,
)

ROOT = Path(__file__).resolve().parents[1]
TELEGRAM_BOT_PATH = ROOT / 'src' / 'telegram_bot.py'

# Порог AST-guard'а: литерал считается дублем единого источника, если
# содержит ≥4 совпадений с ключами настроений (всего 8) или с именами
# жанров кнопок (всего 12). Порог отсекает случайные пары строк (ложные
# срабатывания), но полный или частичный возврат захардкоженного
# списка кнопок ловится всегда (design.md D8, «Риски»).
_MIN_DUPLICATE_NAMES = 4


def _literal_string_values(node: ast.AST) -> Set[str]:
    """Строковые константы литерала: ключи dict / элементы tuple-list.

    Нестроковые и вычисляемые ключи/элементы (f-строки, имена) пропускаются:
    guard ищет именно ХАРДКОД-списки, а динамические конструкции дублём
    источника не являются.
    """
    values: Set[str] = set()
    if isinstance(node, ast.Dict):
        items = [key for key in node.keys if key is not None]
    elif isinstance(node, (ast.Tuple, ast.List)):
        items = list(node.elts)
    else:
        return values
    for item in items:
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            values.add(item.value)
    return values


def find_duplicate_key_literals(source: str) -> List[str]:
    """Найти в исходнике литералы, дублирующие единый источник T10.

    Возвращает описания находок (пустой список — дублей нет). Параметризован
    исходником, а не путём файла: негативный контроль ниже передаёт
    искусственный текст, реальные файлы не правятся (паттерн T13).
    """
    mood_keys = set(MOOD_BUTTON_LABELS)
    genre_names = set(GENRE_MENU_NAMES)
    tree = ast.parse(source)
    found: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Dict, ast.Tuple, ast.List)):
            continue
        values = _literal_string_values(node)
        if not values:
            continue
        mood_overlap = values & mood_keys
        genre_overlap = values & genre_names
        if len(mood_overlap) >= _MIN_DUPLICATE_NAMES:
            found.append(f'строка {node.lineno}: литерал с ключами настроений {sorted(mood_overlap)}')
        elif len(genre_overlap) >= _MIN_DUPLICATE_NAMES:
            found.append(f'строка {node.lineno}: литерал с именами жанров {sorted(genre_overlap)}')
    return found


# === a) Инвариант ключей подписей настроения (критерий приёмки T10) ===


def test_mood_labels_keys_match_mood_dictionaries():
    """Ключи `MOOD_BUTTON_LABELS` == ключи `mood_triggers` == `mood_to_genre`.

    Расширение инварианта B4 (test_mood_audit_b4.py) на третий словарь —
    подписи кнопок: добавление/удаление ключа настроения без обновления
    `MOOD_BUTTON_LABELS` роняет этот тест, а не кнопку в проде.
    """
    dm = make_manager()

    assert set(MOOD_BUTTON_LABELS) == set(dm.mood_triggers), (
        'подписи кнопок разошлись с mood_triggers'
    )
    assert set(MOOD_BUTTON_LABELS) == set(dm.mood_to_genre), (
        'подписи кнопок разошлись с mood_to_genre'
    )
    for key, label in MOOD_BUTTON_LABELS.items():
        assert label.strip(), f'ключ «{key}» с пустой подписью кнопки'


# === b) Инвариант имён жанров кнопок ===


def test_genre_queries_keys_match_menu_names():
    """Ключи `GENRE_QUERIES` в точности равны `GENRE_MENU_NAMES`."""
    assert set(GENRE_QUERIES) == set(GENRE_MENU_NAMES)
    assert len(GENRE_QUERIES) == len(GENRE_MENU_NAMES) == 12, (
        'состав меню жанров изменился — обновите обоснование поднабора '
        'в dialogue_manager.py и ожидания tests/test_genre_keyboard_t6.py'
    )


def test_genre_menu_names_are_subset_of_mood_to_genre_values():
    """Каждое имя жанра кнопок входит в значения `mood_to_genre` (риск T5/T6)."""
    dm = make_manager()
    all_genres = {genre for genres in dm.mood_to_genre.values() for genre in genres}

    missing = sorted(set(GENRE_MENU_NAMES) - all_genres)
    assert not missing, f'имена жанров вне mood_to_genre: {missing}'


# === c) Выводимость запросов из единственного шаблона ===


@pytest.mark.parametrize('name', list(GENRE_MENU_NAMES))
def test_each_genre_query_is_derived_from_single_template(name: str):
    """Запрос жанра выводится из `GENRE_QUERY_TEMPLATE` (риск 5 бэклога T6).

    Правка формулировки возможна ТОЛЬКО через шаблон: ручной override
    отдельного жанра в `GENRE_QUERIES` рассинхронизировал бы классификацию
    (промпты `src/prompts/*.txt` на неё завязаны) и роняет этот тест.
    """
    assert GENRE_QUERIES[name] == GENRE_QUERY_TEMPLATE.format(genre=name)


# === d) Единый источник: импорт, а не копия + AST-guard ===


def test_telegram_bot_uses_imported_objects_not_copies():
    """Атрибуты `telegram_bot` — ТЕ ЖЕ объекты, что в `dialogue_manager`.

    Проверка идентичности (`is`) ловит подмену импорта локальной копией
    словаря/кортежа с теми же значениями: копия прошла бы сравнение `==`,
    но перестала бы быть единым источником (design.md D4 — без алиасов).
    """
    assert telegram_bot.MOOD_BUTTON_LABELS is dialogue_manager.MOOD_BUTTON_LABELS
    assert telegram_bot.GENRE_MENU_NAMES is dialogue_manager.GENRE_MENU_NAMES
    assert telegram_bot.GENRE_QUERIES is dialogue_manager.GENRE_QUERIES


def test_ast_scan_finds_no_local_mood_genre_literals_in_telegram_bot():
    """AST-guard: в telegram_bot.py нет собственных списков ключей/жанров.

    Кодирование критерия приёмки T10: данные кнопок приходят только
    импортом из dialogue_manager.py. Падение теста означает возврат
    хардкод-дубля — удалите литерал и используйте импорт канонических имён,
    а не ослабляйте порог `_MIN_DUPLICATE_NAMES`.
    """
    # `utf-8-sig`: исходники src/ сохранены с BOM, голый utf-8 оставляет
    # U+FEFF в начале текста и ast.parse падает SyntaxError
    # (прецедент — test_button_layout_t7.py).
    source = TELEGRAM_BOT_PATH.read_text(encoding='utf-8-sig')
    duplicates = find_duplicate_key_literals(source)
    assert not duplicates, (
        'в telegram_bot.py найден дубль единого источника T10: '
        + '; '.join(duplicates)
    )


# === Негативный контроль: сканер ОБЯЗАН ловить искусственный дубль ===

# Искусственный исходник с захардкоженным словарём подписей настроения
# (8 ключей — полный дубль источника) и кортежем имён жанров.
_BAD_SOURCE_WITH_DUPLICATES = '''
_MOOD_BUTTON_TEXT_BY_KEY = {
    'грустн': 'Грустное',
    'весел': 'Весёлое',
    'устал': 'Устал',
    'скучно': 'Скучно',
    'страшн': 'Страшное',
    'романт': 'Романтическое',
    'адреналин': 'Адреналин',
    'умный': 'Умное',
}
_GENRE_MENU_NAMES = (
    'комедия', 'драма', 'боевик', 'триллер', 'ужасы', 'фантастика',
)
'''

# Легальный исходник: имена приходят импортом, литеральных списков нет.
_GOOD_SOURCE_WITH_IMPORTS = '''
from dialogue_manager import GENRE_MENU_NAMES, GENRE_QUERIES, MOOD_BUTTON_LABELS

def build():
    labels = [MOOD_BUTTON_LABELS.get(key) for key in ('грустн', 'умный')]
    rows = [GENRE_MENU_NAMES]
    text = GENRE_QUERIES['комедия']
    return labels, rows, text
'''


def test_negative_control_duplicate_literals_are_detected():
    """Сканер находит искусственные дубли обоих видов (guard не «холостой»)."""
    duplicates = find_duplicate_key_literals(_BAD_SOURCE_WITH_DUPLICATES)
    assert any('ключами настроений' in item for item in duplicates), (
        f'литерал ключей настроений НЕ обнаружен: {duplicates}'
    )
    assert any('именами жанров' in item for item in duplicates), (
        f'литерал имён жанров НЕ обнаружен: {duplicates}'
    )


def test_negative_control_imported_names_are_not_flagged():
    """Импорт канонических имён дублем не считается (нет ложных срабатываний)."""
    assert find_duplicate_key_literals(_GOOD_SOURCE_WITH_IMPORTS) == []
