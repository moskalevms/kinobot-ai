"""Регрессионные тесты эмодзи-гайдлайна бота (задача B6, изменение add-emoji-guideline).

Проверяют:
- запретные эмодзи из списка «Избегать» не встречаются в коде src/**/*.py
  и шаблонах src/templates/**/*.html (в промптах и доках они легально
  перечислены в контексте запрета — поэтому сканируются только код/шаблоны);
- декоративные эмодзи без закреплённого смысла не вернулись в telegram_bot.py;
- эмодзи кнопок настроения (T5) встречаются в src/dialogue_manager.py только
  внутри блока объявления `MOOD_BUTTON_LABELS` (guard против возврата к
  декоративному употреблению после вывода 🧠 из `DECORATIVE_EMOJI`; с T10,
  add-single-source-mood-genre-constants-t10, словарь подписей перенесён из
  telegram_bot.py в dialogue_manager.py — скан переведён на новый файл);
- docs/emoji_guideline.md существует и содержит обязательные разделы;
- служебный символ 🏠 (B1/B2, add-menu-hub-exit-buttons-b1b2) закреплён в
  гайдлайне за смыслом «выход в главное меню (хаб)» и совпадает с
  константами подписи/маршрута в src/dialogue_manager.py;
- правило двух стилей кнопок возврата (C2, unify-back-button-emoji-c2):
  🏠 — выход в хаб (ЛЮБАЯ кнопка с `callback_data='menu:main'`), ⬅️ —
  контекстный возврат (`back:list`, `info:{id}` панели оценки, `top:menu`,
  `genre:menu`); проверяются и документ (подраздел «Кнопки возврата: два
  стиля» + таблица-инвентарь), и РЕАЛЬНО собранные клавиатуры всех экранов;
- parameter_extraction_prompt.txt содержит инструкцию о запрете рискованных
  эмодзи и сохранил JSON-контракт извлечения параметров.

Имена эмодзи в сообщениях об ошибках выводятся code point'ами (U+XXXX),
чтобы падение теста не сломало консоль cp1251 (AGENTS.md).
"""
from pathlib import Path
from typing import Any

from conftest import all_buttons, make_list_movies, make_manager, make_movie

ROOT = Path(__file__).resolve().parents[1]

# Запретный набор из гайдлайна (категория «Избегать», задача B6).
BANNED_EMOJI = (
    '\U0001F602',  # 😂 — маркер «устаревшего» бренда
    '\U0001F923',  # 🤣 — то же
    '\U0001F642',  # 🙂 — пассивная агрессия
    '\U0001F44D',  # 👍 — пассивная агрессия
    '\U0001F480',  # 💀 — мем-код
    '\U0001F5FF',  # 🗿 — мем-код
)

# Декоративные эмодзи, удалённые из приветствия/справки решением B6.
# 🧠 (U+1F9E0) ЗДЕСЬ БОЛЬШЕ НЕТ (задача T5, изменение
# add-mood-genre-inline-keyboards-t5t6, design.md D7): B6 снял с него
# декоративность в приветствии, а T5 закрепил за ним ЕДИНСТВЕННЫЙ смысл в
# гайдлайне — «умное настроение», кнопка `mood:pick:умный`. Смысл зафиксирован
# документом, поэтому запрет символа в telegram_bot.py больше не нужен;
# присутствие строки в гайдлайне проверяет
# `test_mood_emoji_are_fixed_in_guideline` ниже (позитивная проверка вместо
# негативной). Остальные символы списка остаются декоративными.
DECORATIVE_EMOJI = (
    '\U0001F916',  # 🤖
    '\U0001F37F',  # 🍿
    '\U0001F447',  # 👇
)

# Эмодзи кнопок клавиатуры выбора настроения (T5): каждый имеет единственный
# закреплённый гайдлайном смысл «кнопка выбора настроения» и не входит ни в
# BANNED_EMOJI, ни в DECORATIVE_EMOJI.
MOOD_BUTTON_EMOJI = (
    '\U0001F614',  # 😔 — грустное настроение
    '\U0001F604',  # 😄 — весёлое настроение
    '\U0001F60C',  # 😌 — усталость
    '\U0001F971',  # 🥱 — скука
    '\U0001F628',  # 😨 — страх/напряжение
    '\U0001F495',  # 💕 — романтическое настроение
    '\U0001F4A5',  # 💥 — адреналин
    '\U0001F9E0',  # 🧠 — «умное» настроение (выведен из DECORATIVE_EMOJI)
)

# Служебный символ навигации (B1/B2, изменение
# add-menu-hub-exit-buttons-b1b2): 🏠 закреплён за единственным смыслом
# «выход в главное меню (хаб)» — кнопки «🏠 Меню» (`menu:main`) в страницах
# «📌 Мой список» и непустой сводке `/stats`. В BANNED_EMOJI и
# DECORATIVE_EMOJI не входит: смысл зафиксирован документом.
HOME_EMOJI = '\U0001F3E0'  # 🏠

# Служебный символ контекстного возврата (C2, unify-back-button-emoji-c2):
# ⬅️ закреплён гайдлайном за смыслом «назад к списку/карточке/родительскому
# подменю раздела». В подписях кнопок используется с вариатором U+FE0F
# («⬅️» = U+2B05 U+FE0F), поэтому проверки идут по базовому символу.
BACK_ARROW = '\u2B05'  # ⬅

# Маршрут хаба: единственный `callback_data`, кнопка с которым ОБЯЗАНА нести
# 🏠 (правило двух стилей, docs/emoji_guideline.md, design.md D1 изменения
# unify-back-button-emoji-c2). Новых маршрутов хаба C2 не вводит.
HUB_CALLBACK = 'menu:main'
# Контекстные маршруты возврата, подпись которых обязана начинаться с ⬅️.
# `info:{id}` проверяется ОТДЕЛЬНЫМ тестом панели оценки: тот же маршрут
# использует кнопка «🎬 Карточка» подтверждения фидбека (B5), которая
# возвратом не является (граница правила — в гайдлайне и design.md D1).
CONTEXTUAL_BACK_CALLBACKS = frozenset({'back:list', 'top:menu', 'genre:menu'})
# Заголовок подраздела гайдлайна с правилом двух стилей (C2).
BACK_BUTTON_SECTION = '## Кнопки возврата: два стиля'
BACK_BUTTON_INVENTORY = '### Инвентарь кнопок возврата'
BACK_BUTTON_DEBT = '### Известное остаточное расхождение'

GUIDELINE_PATH = ROOT / 'docs' / 'emoji_guideline.md'
EXTRACTION_PROMPT_PATH = ROOT / 'src' / 'prompts' / 'parameter_extraction_prompt.txt'
TELEGRAM_BOT_PATH = ROOT / 'src' / 'telegram_bot.py'
# Файл единого источника подписей настроения (T10,
# add-single-source-mood-genre-constants-t10): словарь кнопок перенесён
# сюда, рядом со словарями `mood_triggers`/`mood_to_genre`.
DIALOGUE_MANAGER_PATH = ROOT / 'src' / 'dialogue_manager.py'
# Маркер начала блока объявления словаря подписей кнопок настроения (T5;
# имя и файл — T10). Границы «от строки объявления до закрывающей `}`»
# надёжнее признака «строка-комментарий»: комментарий, документирующий
# словарь, легально содержит пример подписи с эмодзи и обязан оставаться
# разрешённым.
_MOOD_BUTTON_DICT_MARKER = 'MOOD_BUTTON_LABELS: Dict[str, str] = {'


def _describe(char: str) -> str:
    """Представление эмодзи для сообщений об ошибках (без cp1251-сбоев)."""
    return f'U+{ord(char):04X}'


def _describe_text(text: str) -> str:
    """Текст для сообщений assert: символы вне cp1251 заменяются кодпоинтами.

    Русский текст остаётся читаемым, а эмодзи подписей и названий экранов
    (U+1F3E0, U+2B05/U+FE0F, U+1F4CC) выводятся кодпоинтами — иначе падение
    теста ломает консоль cp1251 (AGENTS.md; то же правило, что и в `_describe`).
    """
    parts: list[str] = []
    for char in text:
        try:
            char.encode('cp1251')
        except UnicodeEncodeError:
            parts.append(_describe(char))
        else:
            parts.append(char)
    return ''.join(parts)


def _mood_button_dict_range(lines: list[str]) -> tuple[int, int]:
    """Границы блока словаря подписей настроения (номера строк, 1-based, включительно).

    Начало — строка-маркер объявления вместе с НЕПРЕРЫВНО примыкающим сверху
    блоком комментариев (это документация того же словаря); конец — первая
    строка, состоящая ровно из `}`. Отсутствие маркера или закрывающей скобки
    означает, что структуру словаря сломали, — тест обязан упасть явно.
    """
    start = next(
        (i for i, line in enumerate(lines, 1) if line.strip().startswith(_MOOD_BUTTON_DICT_MARKER)),
        0,
    )
    assert start, f'В src/dialogue_manager.py не найдено объявление {_MOOD_BUTTON_DICT_MARKER!r}'
    while start > 1 and lines[start - 2].strip().startswith('#'):
        start -= 1
    end = next((i for i, line in enumerate(lines, 1) if i > start and line.strip() == '}'), 0)
    assert end, 'Блок MOOD_BUTTON_LABELS не имеет закрывающей «}» на отдельной строке'
    return start, end


def _iter_source_files() -> list[Path]:
    """Файлы кода и шаблонов, подлежащие сканированию (без __pycache__)."""
    files = [*(ROOT / 'src').rglob('*.py'), *(ROOT / 'src' / 'templates').rglob('*.html')]
    return sorted(p for p in files if '__pycache__' not in p.parts)


def test_banned_emoji_absent_in_code_and_templates() -> None:
    """Запретные эмодзи (U+1F602, U+1F923, U+1F642, U+1F44D, U+1F480, U+1F5FF)
    отсутствуют в src/**/*.py и шаблонах."""
    violations: list[str] = []
    for path in _iter_source_files():
        text = path.read_text(encoding='utf-8')
        for line_no, line in enumerate(text.splitlines(), 1):
            for char in BANNED_EMOJI:
                if char in line:
                    rel = path.relative_to(ROOT)
                    violations.append(f'{rel}:{line_no} -> {_describe(char)}')
    assert not violations, 'Запретные эмодзи найдены: ' + '; '.join(violations)


def test_decorative_emoji_absent_in_telegram_bot() -> None:
    """Декоративные эмодзи без закреплённого смысла не используются в telegram_bot.py."""
    text = TELEGRAM_BOT_PATH.read_text(encoding='utf-8')
    found = [
        f'{line_no}:{_describe(char)}'
        for line_no, line in enumerate(text.splitlines(), 1)
        for char in DECORATIVE_EMOJI
        if char in line
    ]
    assert not found, 'Декоративные эмодзи в telegram_bot.py: ' + ', '.join(found)


def test_guideline_exists_and_has_required_sections() -> None:
    """Гайдлайн существует и содержит разделы ядра/добавить/избегать и правила."""
    assert GUIDELINE_PATH.is_file(), 'docs/emoji_guideline.md не найден'
    text = GUIDELINE_PATH.read_text(encoding='utf-8')
    for marker in (
        '1 эмодзи = 1 смысл',
        'Категория «Ядро»',
        'Категория «Добавить»',
        'Категория «Избегать»',
        'Правила употребления',
    ):
        assert marker in text, f'В гайдлайне нет раздела/маркера: {marker!r}'
    # Все эмодзи ядра из бэклога B6 зафиксированы в документе.
    core = (
        '\U0001F3AC',  # 🎬
        '\U0001F4FA',  # 📺
        '\u2B50',      # ⭐
        '\U0001F3C6',  # 🏆
        '\U0001F3AD',  # 🎭
        '\U0001F504',  # 🔄
        '\U0001F4A1',  # 💡
        '\U0001F195',  # 🆕
        '\u26A0',      # ⚠
        '\U0001F345',  # 🍅
    )
    missing = [_describe(c) for c in core if c not in text]
    assert not missing, 'В гайдлайне нет символов ядра: ' + ', '.join(missing)


def test_home_emoji_is_fixed_in_guideline() -> None:
    """🏠 закреплён за смыслом «выход в главное меню (хаб)» ДО кода (B1/B2).

    Изменение `add-menu-hub-exit-buttons-b1b2` добавило кнопку «🏠 Меню»
    (`menu:main`) в страницы списка «📌 Мой список» и непустую сводку
    `/stats`. По конвенции чек-листа B6 строка гайдлайна появляется раньше
    подписи в коде, поэтому проверяются обе стороны: строка таблицы
    «Служебные символы» (смысл + callback) и константы
    `src/dialogue_manager.py`, которые обязаны ей соответствовать.
    """
    text = GUIDELINE_PATH.read_text(encoding='utf-8')
    assert HOME_EMOJI in text, f'В гайдлайне нет символа {_describe(HOME_EMOJI)}'
    lines = [line for line in text.splitlines() if HOME_EMOJI in line]
    # Строка именно ТАБЛИЦЫ служебных символов, а не упоминание в прозе
    assert any(line.startswith(f'| {HOME_EMOJI}') for line in lines), (
        f'Символ {_describe(HOME_EMOJI)} не оформлен строкой таблицы'
    )
    assert any('выход в главное меню' in line for line in lines), (
        f'Смысл {_describe(HOME_EMOJI)} не описан как «выход в главное меню (хаб)»'
    )
    assert any('menu:main' in line for line in lines), (
        f'В строке {_describe(HOME_EMOJI)} не указан callback `menu:main`'
    )
    # Код не разошёлся с документом (скан текста: константы проверяются без
    # импорта src/ — с C2 в файле появились и поведенческие тесты с ЛЕНИВЫМ
    # импортом, см. `_back_button_keyboards`)
    source = (ROOT / 'src' / 'dialogue_manager.py').read_text(encoding='utf-8-sig')
    assert f"MENU_BUTTON_TEXT = '{HOME_EMOJI} Меню'" in source, 'подпись кнопки выхода не соответствует гайдлайну'
    assert "MENU_MAIN_CALLBACK = 'menu:main'" in source, 'цель кнопки выхода не соответствует гайдлайну'


def _guideline_section(marker: str) -> str:
    """Текст раздела гайдлайна от заголовка `marker` до следующего заголовка `## `.

    Подразделы (`### …`) входят в раздел: правило двух стилей кнопок возврата
    содержит инвентарь и блок остаточного расхождения как подразделы.
    """
    lines = GUIDELINE_PATH.read_text(encoding='utf-8').splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(marker)), -1)
    assert start >= 0, f'В docs/emoji_guideline.md нет раздела {marker!r}'
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith('## ')), len(lines))
    return '\n'.join(lines[start:end])


def _inventory_rows(section: str) -> list[tuple[str, str, str, str]]:
    """Строки таблицы-инвентаря кнопок возврата: (экран, подпись, callback, стиль).

    Служебные строки таблицы (заголовок и разделитель) отбрасываются: у
    разделителя первая ячейка состоит из дефисов, у заголовка — «Экран».
    """
    rows: list[tuple[str, str, str, str]] = []
    for line in section.splitlines():
        if not line.startswith('|'):
            continue
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if len(cells) < 4 or cells[0] == 'Экран' or set(cells[0]) <= {'-'}:
            continue
        rows.append((cells[0], cells[1], cells[2], cells[3]))
    return rows


def _watchlist_items(count: int) -> list[dict]:
    """Элементы страницы «📌 Мой список» для инвентаря кнопок возврата."""
    # `conftest.make_list_movies` не переиспользуется: форма данных иная — записи watchlist (`kinopoisk_id`/`title`/`year`) против карточек Кинопоиска списка выдачи.
    return [
        {'kinopoisk_id': 400000 + i, 'title': f'Фильм {i}', 'year': 2020 + i}
        for i in range(1, count + 1)
    ]


def _back_button_keyboards() -> list[tuple[str, Any]]:
    """Инвентарь экранов: имя экрана и СОБРАННАЯ клавиатура (C2).

    Импорт `telegram_bot` и `dialogue_manager` — ЛЕНИВЫЙ, внутри функции
    (паттерн `install_callback_mocks` conftest): модуль бота тянет весь стек
    (load_dotenv, SessionManager, LLM-клиенты), и сбой его импорта не должен
    валить документные проверки этого файла. Все билдеры — чистые функции:
    сеть, БД и LLM не используются, Telegram не опрашивается.
    """
    import dialogue_manager
    import telegram_bot

    stats_with_data = {
        'queries': 3, 'rated': 2, 'avg_rating': 7.4,
        'watched': 1, 'nope': 0, 'watchlist': 1,
    }
    manager = make_manager()
    return [
        ('клавиатура выбора настроения', telegram_bot.build_mood_keyboard()),
        ('подменю топов', telegram_bot.get_top_menu()),
        ('меню жанров', telegram_bot.build_genre_menu()),
        ('универсальный выход из ошибки (B7)', telegram_bot.build_exit_keyboard()),
        ('ошибка сети с повтором карточки', telegram_bot.build_error_keyboard(
            telegram_bot.ERROR_CLASS_NETWORK, 'info:447301')),
        ('неизвестный/устаревший callback', telegram_bot.build_unknown_callback_keyboard()),
        ('подтверждение фидбека', telegram_bot.build_feedback_saved_keyboard(447301)),
        ('карточка фильма', dialogue_manager.build_movie_card_keyboard(make_movie())),
        ('панель оценки', dialogue_manager.build_feedback_rating_keyboard(447301)),
        ('«📌 Мой список»: страница с продолжением', dialogue_manager.render_watchlist_page(
            _watchlist_items(5), 0, 12)[1]),
        ('«📌 Мой список»: последняя страница', dialogue_manager.render_watchlist_page(
            _watchlist_items(2), 5, 7)[1]),
        ('«📌 Мой список»: пустое состояние', dialogue_manager.render_watchlist_page([], 0, 0)[1]),
        ('сводка /stats с данными', telegram_bot.build_stats_response(stats_with_data)[1]),
        ('список выдачи: первая страница', manager.render_movie_list(make_list_movies(7), 'Подборка')[1]),
        ('список выдачи: последняя страница', manager.render_movie_list(
            make_list_movies(7), 'Подборка', offset=2)[1]),
    ]


def test_back_button_two_styles_rule_is_documented() -> None:
    """C2: правило «⬅️ контекстный / 🏠 хаб» зафиксировано в гайдлайне.

    Проверяются формулировка правила (🏠 ⇔ маршрут `menu:main`, ⬅️ ⇔
    контекстный возврат), запрет менять `callback_data`/префиксы маршрутов и
    явная фиксация остаточного расхождения `top:menu`/`genre:menu` — иначе
    расхождение подписи и назначения кнопки снова окажется невидимым.
    """
    section = _guideline_section(BACK_BUTTON_SECTION)
    lines = section.splitlines()

    # 🏠 связан с маршрутом хаба, ⬅️ — с контекстными маршрутами возврата
    assert any(HOME_EMOJI in line and HUB_CALLBACK in line for line in lines), (
        f'Правило не связывает {_describe(HOME_EMOJI)} с маршрутом `{HUB_CALLBACK}`'
    )
    for route in ('back:list', 'info:{id}', 'top:menu', 'genre:menu'):
        assert route in section, f'В правиле не перечислен контекстный маршрут `{route}`'
    assert any(BACK_ARROW in line and route in line for line in lines for route in ('back:list', 'top:menu')), (
        f'Контекстные маршруты не связаны с {_describe(BACK_ARROW)} в одной строке правила'
    )
    # Унификация подписей не меняет маршрутизацию
    assert '_CALLBACK_ROUTES' in section, 'В правиле нет запрета менять префиксы `_CALLBACK_ROUTES`'
    assert 'callback_data' in section and 'запрещено' in section, (
        'В правиле нет запрета менять `callback_data` при унификации подписей'
    )
    # Остаточное расхождение задокументировано как долг, а не потеряно
    assert BACK_BUTTON_DEBT in section, 'Нет подраздела об остаточном расхождении'
    debt = section.split(BACK_BUTTON_DEBT, 1)[1]
    for route in ('top:menu', 'genre:menu'):
        assert route in debt, f'Остаточное расхождение `{route}` не зафиксировано'
    assert 'долг' in debt, 'Расхождение не названо документированным долгом'
    # Инвентарь присутствует и ссылается на код
    assert BACK_BUTTON_INVENTORY in section, 'Нет подраздела с инвентарём кнопок возврата'
    assert 'telegram_bot.py' in section and 'dialogue_manager.py' in section, (
        'В инвентаре не указаны места кнопок в коде'
    )


def test_back_button_inventory_table_matches_two_styles_rule() -> None:
    """Инвентарь гайдлайна внутренне непротиворечив: маршрут ⇒ стиль подписи.

    Каждая строка таблицы-инвентаря проверяется по формальному критерию
    правила (design.md D1): `menu:main` → подпись с 🏠 и стиль «хаб»,
    контекстные маршруты → подпись с ⬅️ и стиль «контекстный». Неизвестный
    маршрут в таблице — ошибка: инвентарь обязан оставаться полным.
    """
    rows = _inventory_rows(_guideline_section(BACK_BUTTON_SECTION))
    assert rows, 'В подразделе правила нет строк таблицы-инвентаря'
    hub_screens: list[str] = []
    contextual_screens: list[str] = []
    for screen, label, callback, style in rows:
        route = callback.strip('`')
        if route == HUB_CALLBACK:
            hub_screens.append(screen)
            assert HOME_EMOJI in label and style.startswith('хаб'), (
                f'Экран «{_describe_text(screen)}»: выход в хаб описан подписью '
                f'«{_describe_text(label)}»/стилем «{_describe_text(style)}»'
            )
            continue
        assert route in CONTEXTUAL_BACK_CALLBACKS or route == 'info:{id}', (
            f'Экран «{_describe_text(screen)}»: в инвентаре неизвестный маршрут '
            f'{_describe_text(route)!r} — дополните правило'
        )
        contextual_screens.append(screen)
        assert BACK_ARROW in label and 'контекстный' in style, (
            f'Экран «{_describe_text(screen)}»: контекстный возврат {_describe_text(route)!r} '
            f'описан подписью «{_describe_text(label)}»/стилем «{_describe_text(style)}»'
        )
    # Инвентарь покрывает обе группы (иначе проверка «зеленеет» впустую)
    assert len(hub_screens) >= 4, (
        f'В инвентаре мало выходов в хаб: {[_describe_text(s) for s in hub_screens]}'
    )
    assert len(contextual_screens) >= 4, (
        f'В инвентаре мало контекстных возвратов: {[_describe_text(s) for s in contextual_screens]}'
    )


def test_back_button_styles_follow_callback_routes() -> None:
    """Инвентарь-тест C2: стиль подписи реальных кнопок определяется маршрутом.

    Проверяются СОБРАННЫЕ клавиатуры всех экранов из инвентаря гайдлайна
    (клавиатура настроения, подменю топов, меню жанров, экраны ошибок и
    выходов B7, карточка фильма, панель оценки, обе ветки страниц «📌 Мой
    список», сводка `/stats` с данными и обе ветки списка выдачи):
    - `callback_data == 'menu:main'` → подпись начинается с 🏠;
    - `back:list` / `top:menu` / `genre:menu` → подпись начинается с ⬅️.

    Скан исходника текстом такое расхождение не ловит: подпись и маршрут
    собираются разными выражениями (design.md D5). Кнопка «🎬 Карточка»
    (`info:{id}`) правила не нарушает — она не возврат (граница правила),
    поэтому маршрут панели оценки проверяется отдельным тестом.
    """
    hub_screens: list[str] = []
    contextual_screens: list[str] = []
    violations: list[str] = []
    for screen, markup in _back_button_keyboards():
        for button in all_buttons(markup):
            data = button.callback_data or ''
            if data == HUB_CALLBACK:
                hub_screens.append(screen)
                if not button.text.startswith(HOME_EMOJI):
                    violations.append(
                        f'{_describe_text(screen)}: `{data}` -> U+{ord(button.text[0]):04X}, '
                        f'ожидается {_describe(HOME_EMOJI)} (выход в хаб)'
                    )
            elif data in CONTEXTUAL_BACK_CALLBACKS:
                contextual_screens.append(screen)
                if not button.text.startswith(BACK_ARROW):
                    violations.append(
                        f'{_describe_text(screen)}: `{data}` -> U+{ord(button.text[0]):04X}, '
                        f'ожидается {_describe(BACK_ARROW)} (контекстный возврат)'
                    )
    assert not violations, 'Нарушения правила двух стилей кнопок возврата: ' + '; '.join(violations)
    # Защита от «пустого» прохождения: обе группы кнопок реально собраны
    assert len(set(hub_screens)) >= 5, (
        f'Выходы в хаб не найдены в клавиатурах: {sorted({_describe_text(s) for s in set(hub_screens)})}'
    )
    assert len(set(contextual_screens)) >= 4, (
        f'Контекстные возвраты не найдены в клавиатурах: '
        f'{sorted({_describe_text(s) for s in set(contextual_screens)})}'
    )


def test_mood_keyboard_exit_uses_shared_hub_constants() -> None:
    """C2: выход в хаб клавиатуры настроения — общая пара констант B1.

    До C2 кнопка вела в главное меню маршрутом `menu:main`, но была подписана
    «⬅️ Назад» — единственное расхождение инвентаря (design.md D2). Проверяются
    подпись (🏠, общая константа `MENU_BUTTON_TEXT` через `truncate_button_text`),
    неизменность маршрута и отсутствие прежней подписи контекстного возврата.
    """
    import telegram_bot

    hub_buttons = [
        b for b in all_buttons(telegram_bot.build_mood_keyboard())
        if b.callback_data == HUB_CALLBACK
    ]
    assert len(hub_buttons) == 1, f'в клавиатуре настроения {len(hub_buttons)} кнопок выхода в хаб'
    button = hub_buttons[0]
    assert button.text == telegram_bot.truncate_button_text(telegram_bot.MENU_BUTTON_TEXT)
    assert button.text.startswith(HOME_EMOJI), f'подпись выхода в хаб: U+{ord(button.text[0]):04X}'
    assert not button.text.startswith(BACK_ARROW), 'в хаб-кнопку вернулась подпись контекстного возврата'
    # Маршрут НЕ изменился: та же цель, что у прочих выходов в хаб
    assert telegram_bot.MENU_MAIN_CALLBACK == HUB_CALLBACK == f'{telegram_bot._MENU_PREFIX}main'
    # Регресс состава клавиатуры: кнопки настроений остались на месте
    mood_buttons = [b for b in all_buttons(telegram_bot.build_mood_keyboard())
                    if (b.callback_data or '').startswith(telegram_bot._MOOD_PICK_PREFIX)]
    assert len(mood_buttons) == len(telegram_bot.dialogue_manager.mood_triggers)


def test_rating_panel_back_button_keeps_contextual_arrow() -> None:
    """«⬅️ Назад» панели оценки (`info:{id}`) — контекстный возврат, ⬅️ сохранён.

    Критерий приёмки C2 п.3: контекстные возвраты не теряют ⬅️ при унификации
    подписей хаб-кнопок.
    """
    import dialogue_manager

    back_buttons = [
        b for b in all_buttons(dialogue_manager.build_feedback_rating_keyboard(447301))
        if (b.callback_data or '').startswith('info:')
    ]
    assert len(back_buttons) == 1, 'панель оценки обязана иметь ровно один возврат к карточке'
    assert back_buttons[0].text.startswith(BACK_ARROW), (
        f'подпись возврата панели оценки: U+{ord(back_buttons[0].text[0]):04X}'
    )
    assert back_buttons[0].text == dialogue_manager.FEEDBACK_BACK_BUTTON_TEXT


def test_mood_emoji_are_fixed_in_guideline() -> None:
    """Позитивная проверка T5: эмодзи кнопок настроения закреплены гайдлайном.

    Замена негативного запрета 🧠 (см. комментарий к `DECORATIVE_EMOJI`):
    символ с единственным закреплённым смыслом перестаёт быть декоративным,
    но обязан присутствовать в документе `docs/emoji_guideline.md` вместе с
    указанием кнопки выбора настроения — иначе «1 эмодзи = 1 смысл» нарушен.
    """
    text = GUIDELINE_PATH.read_text(encoding='utf-8')
    missing = [_describe(c) for c in MOOD_BUTTON_EMOJI if c not in text]
    assert not missing, 'В гайдлайне нет символов настроений T5: ' + ', '.join(missing)
    # Каждый символ описан вместе с его кнопкой (единственный смысл, не декор)
    for char in MOOD_BUTTON_EMOJI:
        lines = [line for line in text.splitlines() if char in line]
        assert any('mood:pick:' in line for line in lines), (
            f'Символ {_describe(char)} в гайдлайне не связан с кнопкой `mood:pick:`'
        )
    # Символы настроений не должны пересекаться с запретными/декоративными
    overlap = {_describe(c) for c in MOOD_BUTTON_EMOJI} & {
        _describe(c) for c in (*BANNED_EMOJI, *DECORATIVE_EMOJI)
    }
    assert not overlap, 'Символ настроения одновременно запретный/декоративный: ' + ', '.join(sorted(overlap))


def test_mood_emoji_used_only_inside_button_dictionary() -> None:
    """Guard T5 (minor 2 ревью): эмодзи настроений — только в подписях кнопок.

    Вывод 🧠 из `DECORATIVE_EMOJI` снял запрет символа в коде, поэтому без
    отдельной проверки возможен возврат к декоративному употреблению (в
    `_WELCOME_HTML`, подписях пунктов меню, текстах ошибок) — нарушение
    принципа «1 эмодзи = 1 смысл». Единственное разрешённое место — блок
    объявления `MOOD_BUTTON_LABELS` вместе с документирующим его комментарием
    (границы ищет `_mood_button_dict_range`). С T10
    (add-single-source-mood-genre-constants-t10) словарь подписей живёт в
    `src/dialogue_manager.py`, поэтому сканируется ОН: из telegram_bot.py
    mood-эмодзи исчезли вовсе — это строже прежнего ограничения, и guard
    продолжает ловить декор в обоих файлах (в telegram_bot.py символы не
    закреплены ни за каким смыслом, а здесь — только за подписями кнопок).
    Файл читается с `utf-8-sig`: исходники src/ сохранены с BOM (прецедент —
    скан dialogue_manager.py в `test_home_emoji_is_fixed_in_guideline`).
    """
    lines = DIALOGUE_MANAGER_PATH.read_text(encoding='utf-8-sig').splitlines()
    start, end = _mood_button_dict_range(lines)

    violations = [
        f'{line_no}:{_describe(char)}'
        for line_no, line in enumerate(lines, 1)
        if not start <= line_no <= end
        for char in MOOD_BUTTON_EMOJI
        if char in line
    ]
    assert not violations, (
        f'Эмодзи настроений вне блока MOOD_BUTTON_LABELS (строки {start}-{end}): '
        + ', '.join(violations)
    )
    # Позитивная часть guard'а: все восемь символов действительно используются
    # в словаре подписей (иначе guard «пуст» и кнопки остались бы без эмодзи)
    block = '\n'.join(lines[start - 1:end])
    missing = [_describe(c) for c in MOOD_BUTTON_EMOJI if c not in block]
    assert not missing, 'В словаре подписей кнопок настроения нет символов: ' + ', '.join(missing)


def test_extraction_prompt_has_emoji_ban_and_keeps_json_contract() -> None:
    """Промпт извлечения параметров запрещает рискованные эмодзи, JSON-контракт цел."""
    text = EXTRACTION_PROMPT_PATH.read_text(encoding='utf-8')
    # Инструкция о запрете присутствует и перечисляет весь запретный набор.
    assert 'ЗАПРЕЩЕНО' in text, 'В промпте нет строки запрета (маркер «ЗАПРЕЩЕНО»)'
    assert '1 эмодзи = 1 смысл' in text, 'В промпте нет принципа «1 эмодзи = 1 смысл»'
    missing = [_describe(c) for c in BANNED_EMOJI if c not in text]
    assert not missing, 'Промпт не перечисляет запретные эмодзи: ' + ', '.join(missing)
    # Оговорка, что формат ответа не меняется.
    assert 'чистый JSON' in text, 'В промпте нет оговорки о сохранении формата JSON'
    # JSON-контракт извлечения параметров сохранён (поля и примеры).
    assert 'Формат ответа:' in text
    for field_line in (
        'intent, target_movie, genre, year, year_range, actor, director, studio, '
        'country, mood, count, min_rating, movie_type, critics_approved',
    ):
        assert field_line in text, 'Список полей JSON-ответа изменён'
    assert text.count('{"intent":') >= 8, 'Примеры JSON-ответов повреждены'
