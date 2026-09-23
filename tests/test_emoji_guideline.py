"""Регрессионные тесты эмодзи-гайдлайна бота (задача B6, изменение add-emoji-guideline).

Проверяют:
- запретные эмодзи из списка «Избегать» не встречаются в коде src/**/*.py
  и шаблонах src/templates/**/*.html (в промптах и доках они легально
  перечислены в контексте запрета — поэтому сканируются только код/шаблоны);
- декоративные эмодзи без закреплённого смысла не вернулись в telegram_bot.py;
- docs/emoji_guideline.md существует и содержит обязательные разделы;
- parameter_extraction_prompt.txt содержит инструкцию о запрете рискованных
  эмодзи и сохранил JSON-контракт извлечения параметров;
- single_movie_response_prompt.txt содержит правило эмодзи и пометку о том,
  что файл пока не загружается кодом.

Имена эмодзи в сообщениях об ошибках выводятся code point'ами (U+XXXX),
чтобы падение теста не сломало консоль cp1251 (AGENTS.md).
"""
from pathlib import Path

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
DECORATIVE_EMOJI = (
    '\U0001F916',  # 🤖
    '\U0001F37F',  # 🍿
    '\U0001F447',  # 👇
    '\U0001F9E0',  # 🧠
)

GUIDELINE_PATH = ROOT / 'docs' / 'emoji_guideline.md'
EXTRACTION_PROMPT_PATH = ROOT / 'src' / 'prompts' / 'parameter_extraction_prompt.txt'
SINGLE_MOVIE_PROMPT_PATH = ROOT / 'src' / 'prompts' / 'single_movie_response_prompt.txt'


def _describe(char: str) -> str:
    """Представление эмодзи для сообщений об ошибках (без cp1251-сбоев)."""
    return f'U+{ord(char):04X}'


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
    text = (ROOT / 'src' / 'telegram_bot.py').read_text(encoding='utf-8')
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


def test_single_movie_prompt_has_emoji_rule_and_status_note() -> None:
    """Мёртвый промпт ответа согласован с гайдлайном и помечен как незагружаемый."""
    text = SINGLE_MOVIE_PROMPT_PATH.read_text(encoding='utf-8')
    assert 'не загружается' in text, 'Нет пометки о том, что файл не загружается кодом'
    assert 'ЗАПРЕЩЕНО' in text, 'В промпте нет строки запрета (маркер «ЗАПРЕЩЕНО»)'
    assert 'не более одного эмодзи' in text, 'Нет правила «не более одного эмодзи»'
    # Прежняя инструкция «1–2 релевантных эмодзи» и пример с 😊🍿 удалены.
    assert '1–2 релевантных эмодзи' not in text, 'Старое правило «1–2 эмодзи» не удалено'
    assert '\U0001F60A' not in text, 'Пример промпта всё ещё содержит U+1F60A'
