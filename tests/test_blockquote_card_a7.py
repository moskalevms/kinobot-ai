"""Тесты A7: прогрессивное раскрытие карточки (add-blockquote-card-and-edit-callbacks).

Без сети: проверяются структура карточки «жирный вердикт + описание в
`<blockquote expandable>`, бюджет caption 1024 с учётом обёртки цитаты
(37 символов), гарантированный закрывающий тег при обрезке, экранирование
пользовательского HTML внутри цитаты и хелпер спойлера `wrap_spoiler`
(точка расширения — к описанию Кинопоиска не подключается).
Мок-паттерны — общие фейки и хелперы из `tests/conftest.py` (nit n4,
изменение verify-phase0-uiux-tests).
"""
from conftest import (
    LONG_DESCRIPTION,
    assert_html_balanced,
    make_movie,
)
from conftest import BROKEN_ENTITY_RE as _BROKEN_ENTITY_RE
from dialogue_manager import (
    CARD_QUOTE_CLOSE,
    CARD_QUOTE_OPEN,
    CARD_QUOTE_OVERHEAD,
    CARD_QUOTE_SEPARATOR,
    CARD_SPOILER_CLOSE,
    CARD_SPOILER_OPEN,
    MOVIE_CARD_CAPTION_LIMIT,
    _fit_card_head,
    build_movie_card,
    format_movie_card,
    wrap_spoiler,
)


def _movie(**overrides) -> dict:
    """Фильм A7: базовый словарь A4 плюс rt_score=91 по умолчанию.

    Обёртка над общей фабрикой `make_movie` (design.md D2): набор полей
    участвует в побайтовых assert'ах вердикта и не должен меняться.
    """
    overrides.setdefault('rt_score', 91)
    return make_movie(**overrides)


# --- 5.1 Разметка вердикта и цитаты ---


def test_verdict_is_first_line_and_fully_bold():
    """Вердикт — первая строка карточки, ЦЕЛИКОМ в одном <strong> (🎬 вне тега)."""
    card = format_movie_card(_movie())

    lines = card.split(CARD_QUOTE_SEPARATOR)
    verdict = lines[0]
    assert verdict.startswith('🎬 <strong>')
    assert verdict.endswith('</strong>')
    # Состав вердикта: название, год, жанр, рейтинг, 🍅-бейдж
    for expected in ('Дюна', '(2021)', 'фантастика', 'с рейтингом 7.8', ' · 🍅 91%.'):
        assert expected in verdict
    # Внутри вердикта ровно один <strong> — вложенных тегов жирного нет
    assert verdict.count('<strong>') == 1


def test_description_wrapped_in_expandable_blockquote():
    """Описание — в сворачиваемой цитате отдельной строкой под вердиктом."""
    card = format_movie_card(_movie(description='Сон внутри сна'))

    assert f'{CARD_QUOTE_SEPARATOR}{CARD_QUOTE_OPEN}Сон внутри сна{CARD_QUOTE_CLOSE}' in card
    assert card.endswith(CARD_QUOTE_CLOSE)
    assert_html_balanced(card)


def test_empty_description_has_no_blockquote():
    """Пустое описание (None/''/пробелы) — вердикт без пустой цитаты."""
    for description in (None, '', '   ', '\t\n'):
        card = format_movie_card(_movie(description=description))

        assert '<blockquote' not in card
        assert CARD_QUOTE_CLOSE not in card
        assert_html_balanced(card)


def test_verdict_without_rt_score_has_no_badge_but_stays_bold():
    card = format_movie_card(_movie(rt_score=None))

    assert '🍅' not in card
    assert card.startswith('🎬 <strong>')
    assert_html_balanced(card)


# --- 5.2 Бюджет caption с учётом обёртки ---


def test_quote_overhead_is_37_chars():
    """Обёртка цитаты с разделителем = 37 символов (оценка задания A7)."""
    assert CARD_QUOTE_OVERHEAD == 37
    assert CARD_QUOTE_OVERHEAD == len(CARD_QUOTE_OPEN) + len(CARD_QUOTE_SEPARATOR) + len(CARD_QUOTE_CLOSE)


def test_long_description_fits_budget_with_wrapper():
    """Описание 5000+ символов: итог ≤1024, цитата закрыта, «…» перед тегом."""
    text, _ = build_movie_card(_movie(description=LONG_DESCRIPTION))

    assert len(LONG_DESCRIPTION) > 5000
    assert len(text) <= MOVIE_CARD_CAPTION_LIMIT
    assert text.endswith(f'…{CARD_QUOTE_CLOSE}')
    assert_html_balanced(text)
    # Вердикт и RT-бейдж сохранены (обрезка только по описанию)
    assert text.startswith('🎬 <strong>')
    assert '🍅 91%' in text


def test_no_broken_entities_inside_quote_after_truncation():
    text, _ = build_movie_card(_movie(description=LONG_DESCRIPTION))

    assert _BROKEN_ENTITY_RE.search(text) is None
    # Инъекция тегов через описание невозможна: символы экранированы
    assert '<b>важно</b>' not in text
    assert '&lt;b&gt;важно&lt;/b&gt;' in text


def test_pathological_head_fits_budget_and_keeps_rating_and_badge():
    """Патологическая шапка: жанр укорачивается, рейтинг и бейдж сохраняются.

    С A7 вердикт целиком внутри <strong> — голый срез оставил бы тег
    незакрытым; `_fit_card_head` укорачивает жанр и (в остатке) режет
    через `_cut_verdict_safely` с явным закрытием тега.
    """
    huge_genre = 'фантастика&приключения&боевик& ' * 200
    movie = _movie(title='X', genre=huge_genre, description='')
    assert len(format_movie_card({**movie, 'description': ''})) > MOVIE_CARD_CAPTION_LIMIT

    head = _fit_card_head(movie, MOVIE_CARD_CAPTION_LIMIT)
    assert len(head) <= MOVIE_CARD_CAPTION_LIMIT

    text, _ = build_movie_card(movie)
    assert len(text) <= MOVIE_CARD_CAPTION_LIMIT
    assert_html_balanced(text)
    assert text.startswith('🎬 <strong>')
    # Рейтинг и RT-бейдж в хвосте вердикта НЕ потеряны (жанр укорочен первым)
    assert 'с рейтингом 7.8' in text
    assert '🍅 91%' in text
    assert '</stro' not in text or '</strong>' in text
    assert _BROKEN_ENTITY_RE.search(text) is None


def test_budget_invariant_holds_for_many_limits():
    """Инвариант len(text) <= limit — на разных limit и разных данных."""
    movies = [
        _movie(description=LONG_DESCRIPTION),
        _movie(description='Короткое', title='Название' * 200),
        _movie(description='Среднее' * 100, genre='жанр&жанр' * 100, rating='7.8' * 50),
        _movie(description=''),
        {'id': 1, 'title': 'Минимум'},
    ]
    for limit in (10, 20, 37, 38, 40, 64, 100, 300, 1024):
        for movie in movies:
            text, _ = build_movie_card(movie, limit=limit)
            assert len(text) <= limit, f'limit={limit}, movie={movie.get("title")}'
            assert_html_balanced(text)


def test_empty_quote_is_never_emitted_when_head_eats_budget():
    """Шапка съела бюджет — цитаты нет ЛИБО в ней есть содержимое, не только «…» (n3/m1)."""
    movie = _movie(title='Название' * 300, description=LONG_DESCRIPTION)

    text, _ = build_movie_card(movie, limit=64)

    assert len(text) <= 64
    assert_html_balanced(text)
    if CARD_QUOTE_OPEN in text:
        inner = text.split(CARD_QUOTE_OPEN, 1)[1]
        assert inner.endswith(CARD_QUOTE_CLOSE)
        content = inner.removesuffix(CARD_QUOTE_CLOSE)
        # Бессодержательная цитата (пустая или из одного маркера обрезки) недопустима
        assert content and content != '…'


def test_no_quote_of_only_ellipsis():
    """Репродукция ревью m1: после среза остался бы один «…» — цитата не выводится.

    Конфигурация, в которой старая логика выдавала
    «вердикт\\n<blockquote expandable>…</blockquote>»: бюджет описания
    равен 2, а безопасный срез целиком съедается откатом от разорванной
    HTML-сущности («&amp;amp;…» → «»).
    """
    movie = _movie(title='T', genre='g', rating='7', year=2020, description='&amp;' * 40)

    text, _ = build_movie_card(movie, limit=42)

    assert len(text) <= 42
    assert f'{CARD_QUOTE_OPEN}…{CARD_QUOTE_CLOSE}' not in text
    assert CARD_QUOTE_OPEN not in text  # содержимого нет — цитаты нет вовсе
    assert_html_balanced(text)
    assert _BROKEN_ENTITY_RE.search(text) is None


def test_verdict_normalizes_missing_fields():
    """m4: пропущенные поля — «—»/опущенный «(год)», литералов «None» нет."""
    card = format_movie_card({
        'id': 1, 'title': None, 'year': None,
        'genre': '', 'rating': None, 'description': None,
    })

    assert 'None' not in card
    assert '()' not in card
    assert '  ' not in card  # висящих двойных разделителей нет
    assert card == '🎬 <strong>— — — с рейтингом —.</strong>'


def test_verdict_omits_year_part_when_year_missing():
    """Год None/'' — часть «(год)» опускается ЦЕЛИКОМ (паттерн строки списка A2)."""
    for year in (None, ''):
        card = format_movie_card(_movie(year=year, rt_score=None))

        assert '()' not in card
        assert '(None)' not in card
        assert card.startswith('🎬 <strong>Дюна — фантастика с рейтингом 7.8.</strong>')


def test_verdict_keeps_zero_rating():
    """rating=0 — валидные данные: «—» только для None/'' (как в строке списка)."""
    card = format_movie_card(_movie(rating=0, rt_score=None))

    assert 'с рейтингом 0.' in card


def test_build_matches_format_for_short_description_and_none():
    """Побайтовая идентичность сборки и форматтера (короткое описание и None)."""
    movie = _movie(description='Сон внутри сна')
    assert build_movie_card(movie)[0] == format_movie_card(movie)

    none_movie = _movie(description=None)
    assert build_movie_card(none_movie)[0] == format_movie_card(none_movie)
    assert '<blockquote' not in build_movie_card(none_movie)[0]


# --- 5.3 Экранирование и спойлер ---


def test_special_chars_in_description_do_not_break_quote():
    """Спецсимволы описания экранированы ВНУТРИ цитаты — parse не ломается."""
    description = '<script>alert("x")</script> & <b>важно</b> ' * 50

    text, _ = build_movie_card(_movie(description=description))

    assert len(text) <= MOVIE_CARD_CAPTION_LIMIT
    assert '<script>' not in text
    assert '&lt;script&gt;' in text
    assert text.endswith(CARD_QUOTE_CLOSE)
    assert_html_balanced(text)
    assert _BROKEN_ENTITY_RE.search(text) is None


def test_wrap_spoiler_helper():
    """Хелпер спойлера: экранирование + обёртка; пустое значение → пустая строка."""
    assert wrap_spoiler('Он <умер> & она "ушла"') == (
        f'{CARD_SPOILER_OPEN}Он &lt;умер&gt; &amp; она &quot;ушла&quot;{CARD_SPOILER_CLOSE}'
    )
    for empty in ('', None, 0):
        assert wrap_spoiler(empty) == ''
    assert_html_balanced(wrap_spoiler('текст'))


def test_spoiler_is_not_applied_to_description_heuristically():
    """Точка расширения НЕ подключена: описание Кинопоиска неделимо."""
    text, _ = build_movie_card(_movie(description='Неожиданно он оказался мертв. Финал.'))

    assert 'tg-spoiler' not in text
    assert CARD_SPOILER_OPEN not in text
