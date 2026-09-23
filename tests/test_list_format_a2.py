"""Тесты формата списка выдачи A2 (изменение add-rich-movie-list-format).

Без сети: проверяются отображаемый лимит 5, новый формат строки списка
(название-ссылка на Кинопоиск, год, жанр/страна, рейтинг с источником),
плавная деградация при неполных данных (без висящих « · » и пустых
скобок), HTML-экранирование и сохранение полного списка в сессии и
movies_list (лимит добычи данных не равен лимиту отображения).
"""
import asyncio
from unittest.mock import AsyncMock

from conftest import make_manager as _manager
from conftest import make_movie
from dialogue_manager import (
    LIST_DISPLAY_LIMIT,
    format_list_line,
)


def _movie(**overrides) -> dict:
    """Фильм A2: без description/poster_url, с явным rt_score=None.

    Обёртка над общей фабрикой `make_movie` (design.md D2): набор полей
    участвует в побайтовых assert'ах `format_list_line` и не должен меняться.
    Ключи выкидываются ДО применения overrides (nit n5 ревью R1): явная
    передача description/poster_url в тесте не игнорируется.
    """
    movie = make_movie()
    movie.pop('description', None)
    movie.pop('poster_url', None)
    movie['rt_score'] = None
    movie.update(overrides)
    return movie


def _run(coro):
    return asyncio.run(coro)


# --- (а) Отображаемый лимит: 13 на входе → 5 в тексте и 5 кнопок ---


def test_display_limit_constant_is_five():
    assert LIST_DISPLAY_LIMIT == 5


def test_thirteen_movies_render_only_five():
    dm = _manager()
    movies = [_movie(id=i, title=f'Фильм {i}') for i in range(1, 14)]

    response, keyboard = dm._generate_list_response(movies, 'Заголовок')

    # Заголовок + ровно 5 строк фильмов
    assert len(response.splitlines()) == 6
    # A3: два ряда номеров (4 + 1) и отдельный ряд навигации под ними
    assert len(keyboard.inline_keyboard) == 3
    for i in range(1, 6):
        # Название — ссылка (kinopoisk_url в _movie присутствует)
        assert f'{i}. <b><a href=' in response
        assert f'>Фильм {i}</a></b>' in response
    # Шестой и далее фильмы не отображаются
    assert '>Фильм 6</a></b>' not in response
    assert '>Фильм 13</a></b>' not in response
    # Кнопки — компактные номера (A3): подпись не зависит от названия,
    # callback прежний — info:{id} СООТВЕТСТВУЮЩЕГО фильма
    number_buttons = [b for row in keyboard.inline_keyboard[:2] for b in row]
    assert [b.text for b in number_buttons] == ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣']
    assert [b.callback_data for b in number_buttons] == [
        f'info:{i}' for i in range(1, 6)
    ]
    # Ряд навигации — отдельный, под номерами (B2: три контекстных quick
    # replies — другие варианты / следующая страница / случайный фильм)
    nav_row = keyboard.inline_keyboard[-1]
    assert [b.text for b in nav_row] == ['🔄 Другие', '⬇️ Ещё 5', '🎲 Случайный']
    assert nav_row[0].callback_data == 'alt:list'
    assert nav_row[1].callback_data.startswith('page:5:')
    assert nav_row[2].callback_data == 'random:movie'


def test_fewer_movies_than_limit_all_rendered():
    """Лимит — верхняя граница, а не квота: 3 фильма → 3 в списке."""
    dm = _manager()
    movies = [_movie(id=i, title=f'Фильм {i}') for i in range(1, 4)]

    response, keyboard = dm._generate_list_response(movies, 'Заголовок')

    assert len(response.splitlines()) == 4
    # A3: один ряд номеров (3 кнопки) + ряд навигации
    assert len(keyboard.inline_keyboard) == 2
    assert len(keyboard.inline_keyboard[0]) == 3


def test_full_list_stays_in_session_and_movies_list():
    """Лимит отображения не усекает данные: сессия и movies_list — 13."""
    dm = _manager()
    dm.intent_classifier.classify_with_llm = AsyncMock(
        return_value={'intent': 'initial', 'genre': 'комедия',
                      'movie_type': 'movie'}
    )
    movies = [_movie(id=i, title=f'Фильм {i}') for i in range(1, 14)]
    dm.movie_agent.recommend_movies = AsyncMock(return_value=movies)

    result = _run(dm.process_message(None, 'u1', 'посоветуй комедию'))

    # Данные не усечены: полный список в результате и в сессии
    assert len(result['movies_list']) == 13
    session = dm.session_manager.get_session('u1')
    assert len(session.last_movies) == 13
    # Отображение — 5 фильмов (заголовок + 5 строк)
    assert len(result['response'].splitlines()) == 6
    # A3: два ряда номеров (4 + 1) + ряд навигации
    assert len(result['reply_markup'].inline_keyboard) == 3


# --- (б) Полный формат строки ---


def test_full_line_format_with_link_genre_country_source_and_badge():
    line = format_list_line(1, _movie(rt_score=83))

    assert line == (
        '1. <b><a href="https://www.kinopoisk.ru/film/435/">Дюна</a></b>'
        ' (2021) · фантастика, США · ⭐ 7.8 (IMDB) · 🍅 83%'
    )


def test_line_format_with_kp_source_and_without_badge():
    """Источник «КП» выводится дословно; без rt_score бейджа нет."""
    line = format_list_line(2, _movie(rating=8.6, rating_source='КП'))

    assert line == (
        '2. <b><a href="https://www.kinopoisk.ru/film/435/">Дюна</a></b>'
        ' (2021) · фантастика, США · ⭐ 8.6 (КП)'
    )


# --- (в) Деградация при неполных данных ---


def test_degradation_without_url_genre_country_and_source():
    """Без ссылки/жанра/страны/источника — нет висящих « · » и скобок."""
    line = format_list_line(1, _movie(
        kinopoisk_url=None, genre='', country=None, rating_source='—',
    ))

    assert line == '1. <b>Дюна</b> (2021) · ⭐ 7.8'


def test_degradation_only_country_without_genre():
    """Есть только страна — выводится она, без запятой и пустого жанра."""
    line = format_list_line(1, _movie(genre='', country='Россия'))

    assert ' · Россия · ' in line
    assert ', ' not in line.split(' · ')[1]


def test_degradation_only_genre_without_country():
    line = format_list_line(1, _movie(genre='драма', country=''))

    assert ' · драма · ' in line


def test_invalid_rating_hides_source_parens():
    """Рейтинг «—»: скобки с источником не выводятся вовсе."""
    line = format_list_line(1, _movie(rating='—'))

    assert '⭐ —' in line
    assert '(IMDB)' not in line
    assert '()' not in line


def test_minimal_movie_dict_is_safe():
    """Старый кэш/минимальный словарь: нет KeyError и висящих разделителей."""
    line = format_list_line(1, {'id': 1, 'title': 'Минимум'})

    assert line == '1. <b>Минимум</b> · ⭐ —'


# --- (г) HTML-экранирование названия и href ---


def test_html_escaping_in_title_and_href():
    line = format_list_line(1, _movie(
        title='A <b>&"x"',
        kinopoisk_url='https://www.kinopoisk.ru/film/1/?q="x"&y=1',
    ))

    # Название экранировано: инъекция тегов невозможна
    assert 'A &lt;b&gt;&amp;&quot;x&quot;' in line
    assert '<b>&"x"' not in line
    # Кавычки и амперсанд в href экранированы (quote=True)
    assert 'href="https://www.kinopoisk.ru/film/1/?q=&quot;x&quot;&amp;y=1"' in line


def test_genre_country_and_rating_source_are_escaped():
    line = format_list_line(1, _movie(
        genre='драма <i>', country='США & Канада', rating_source='КП"б"',
    ))

    assert 'драма &lt;i&gt;, США &amp; Канада' in line
    assert 'КП&quot;б&quot;' in line


# --- (д) Год отсутствует — скобок «()» нет ---


def test_missing_year_has_no_parens():
    for year in (None, ''):
        line = format_list_line(1, _movie(year=year))

        assert '()' not in line
        assert '(2021)' not in line
        assert line.startswith(
            '1. <b><a href="https://www.kinopoisk.ru/film/435/">Дюна</a></b>'
            ' · фантастика, США · ⭐ 7.8 (IMDB)'
        )
