"""Тесты C3/C4: трейлер и стриминг-провайдеры в карточке фильма.

Изменение add-trailer-and-provider-buttons (фаза 2, Epic C):
- C3 — extract_trailer_url: защитный парсинг videos.trailers (API v1.4
  сегодня поле не заполняет — разведка 2026-09-24, механизм
  forward-compatible), кнопка «▶️ Трейлер» только при валидной ссылке;
- C4 — extract_watch_providers: парсинг watchability.items (непустые
  name/url http, дедуп по name, лимит WATCH_PROVIDERS_LIMIT=3) и ряд
  url-кнопок провайдеров в клавиатуре карточки ПЕРЕД рядом фидбека B5;
- поля trailer_url/watch_providers во всех точках форматирования
  (formatted_movie движка, полный документ callback info:{id});
- selectFields списковых запросов содержит videos и watchability;
- graceful-деградация: без данных клавиатура побайтово прежняя.

Все тесты на мок-данных, БЕЗ реальных сетевых вызовов (паттерны
tests/conftest.py).
"""
import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

import telegram_bot
from conftest import (
    FakeCallbackUpdate,
    FakeChat,
    FakeMessage,
    FakeUser,
    all_buttons,
    install_callback_mocks,
    make_manager,
    make_movie,
)
from dialogue_manager import (
    CARD_BACK_BUTTON_TEXT,
    CARD_LINK_BUTTON_TEXT,
    CARD_SAVE_BUTTON_TEXT,
    CARD_SIMILAR_BUTTON_TEXT,
    CARD_TRAILER_BUTTON_TEXT,
    FEEDBACK_NOPE_BUTTON_TEXT,
    FEEDBACK_RATE_BUTTON_TEXT,
    FEEDBACK_WATCHED_BUTTON_TEXT,
    build_movie_card_keyboard,
)
from kinopoisk_client import KinopoiskClient
from recommendation_engine import RecommendationEngine
from utils.movie_filter import (
    PROVIDER_URL_MAX_LENGTH,
    WATCH_PROVIDERS_LIMIT,
    extract_trailer_url,
    extract_watch_providers,
)

TRAILER_URL = 'https://www.youtube.com/watch?v=YoHD9XEg0xA'
PROVIDER_OKKO = {'name': 'Okko', 'url': 'https://okko.tv/movie/1?utm=x'}
PROVIDER_KION = {'name': 'KION', 'url': 'https://kion.ru/m/1?utm=x'}
PROVIDER_IVI = {'name': 'Иви', 'url': 'https://www.ivi.ru/watch/1?utm=x'}


# --- 1. extract_trailer_url (C3) ---


def test_trailer_url_valid_first_element():
    movie = {'videos': {'trailers': [{'url': TRAILER_URL, 'name': 'Трейлер', 'site': 'youtube'}]}}
    assert extract_trailer_url(movie) == TRAILER_URL


def test_trailer_url_youtube_priority_over_first_non_youtube():
    """Приоритет site == 'youtube' (регистронезависимо), даже если он не первый."""
    movie = {'videos': {'trailers': [
        {'url': 'https://kinopoisk.ru/trailer.mp4', 'name': 'КП'},
        {'url': TRAILER_URL, 'name': 'YouTube', 'site': 'YouTuBe'},
    ]}}
    assert extract_trailer_url(movie) == TRAILER_URL


def test_trailer_url_falls_back_to_first_valid_without_site():
    """Поля site нет ни у кого — берётся первая валидная ссылка."""
    movie = {'videos': {'trailers': [
        {'url': 'https://first.example/video.mp4'},
        {'url': 'https://second.example/video.mp4'},
    ]}}
    assert extract_trailer_url(movie) == 'https://first.example/video.mp4'


def test_trailer_url_skips_invalid_elements():
    """Не-словари и битые ссылки пропускаются, берётся следующая валидная."""
    movie = {'videos': {'trailers': [
        'не-словарь',
        {'name': 'без url'},
        {'url': None},
        {'url': 'ftp://example.com/video.mp4'},
        {'url': '  ' + TRAILER_URL + '  '},
    ]}}
    assert extract_trailer_url(movie) == TRAILER_URL


@pytest.mark.parametrize('movie', [
    {},                                        # videos отсутствует
    {'videos': None},                          # videos = None
    {'videos': 'строка'},                      # videos не словарь
    {'videos': {}},                            # trailers отсутствует
    {'videos': {'trailers': None}},            # trailers = None
    {'videos': {'trailers': 'не-список'}},     # trailers не список
    {'videos': {'trailers': []}},              # пустой список
    {'videos': {'trailers': ['x', 42]}},       # только не-словари
    {'videos': {'trailers': [{'url': 'ftp://x'}]}},  # только битые url
])
def test_trailer_url_degrades_to_none(movie: Dict[str, Any]):
    """Любая патология данных — None без исключений (незаметная деградация)."""
    assert extract_trailer_url(movie) is None


# --- 2. extract_watch_providers (C4) ---


def _six_providers() -> List[Dict[str, str]]:
    """Реалистичный набор из разведки 2026-09-24 (фильм 535341, «1+1»)."""
    return [
        {'name': 'Okko', 'url': 'https://okko.tv/movie/535341'},
        {'name': 'KION', 'url': 'https://kion.ru/m/535341'},
        {'name': 'Иви', 'url': 'https://www.ivi.ru/watch/535341'},
        {'name': 'START', 'url': 'https://start.ru/watch/535341'},
        {'name': 'PREMIER', 'url': 'https://premier.one/show/535341'},
        {'name': 'Kinopoisk HD', 'url': 'https://hd.kinopoisk.ru/film/535341'},
    ]


def test_watch_providers_limit_and_order():
    """6 валидных провайдеров — срез первых WATCH_PROVIDERS_LIMIT, порядок сохранён."""
    assert WATCH_PROVIDERS_LIMIT == 3
    movie = {'watchability': {'items': _six_providers()}}
    providers = extract_watch_providers(movie)
    assert providers == _six_providers()[:WATCH_PROVIDERS_LIMIT]
    assert [p['name'] for p in providers] == ['Okko', 'KION', 'Иви']


def test_watch_providers_dedup_by_name_preserves_first_order():
    movie = {'watchability': {'items': [
        PROVIDER_OKKO,
        PROVIDER_KION,
        {'name': 'Okko', 'url': 'https://okko.tv/other?utm=y'},  # дубль имени
        PROVIDER_IVI,
    ]}}
    providers = extract_watch_providers(movie)
    assert providers == [PROVIDER_OKKO, PROVIDER_KION, PROVIDER_IVI]


def test_watch_providers_filters_invalid_items():
    """Не-словари, пустые name, url без http и не-строки отбрасываются."""
    movie = {'watchability': {'items': [
        'не-словарь',
        {'name': '', 'url': 'https://empty-name.ru'},        # пустое имя
        {'name': '   ', 'url': 'https://blank-name.ru'},     # имя из пробелов
        {'name': 'БезUrl'},                                   # url отсутствует
        {'name': 'Ftp', 'url': 'ftp://ftp.example'},          # не http
        {'name': 42, 'url': 'https://not-a-str.ru'},          # name не строка
        {'name': 'Иви', 'url': PROVIDER_IVI['url']},
    ]}}
    providers = extract_watch_providers(movie)
    assert providers == [PROVIDER_IVI]


def test_watch_providers_strips_name_and_url():
    movie = {'watchability': {'items': [{'name': '  Okko  ', 'url': '  ' + PROVIDER_OKKO['url']}]}}
    assert extract_watch_providers(movie) == [PROVIDER_OKKO]


def test_watch_providers_rejects_url_longer_than_bot_api_limit():
    """Url длиннее 256 символов (лимит Bot API) отсеивается: иначе
    BadRequest(ButtonUrlInvalid) потерял бы всю карточку (minor ревью)."""
    assert PROVIDER_URL_MAX_LENGTH == 256
    long_url = 'https://okko.tv/movie/535341?' + 'u' * 300
    assert len(long_url) > PROVIDER_URL_MAX_LENGTH
    movie = {'watchability': {'items': [
        {'name': 'Okko', 'url': long_url},
        PROVIDER_KION,
    ]}}
    assert extract_watch_providers(movie) == [PROVIDER_KION]


def test_watch_providers_accepts_url_exactly_at_limit():
    """Url ровно 256 символов — граничное значение, проходит."""
    base = 'https://okko.tv/movie/535341?'
    url_256 = base + 'u' * (PROVIDER_URL_MAX_LENGTH - len(base))
    assert len(url_256) == PROVIDER_URL_MAX_LENGTH
    movie = {'watchability': {'items': [{'name': 'Okko', 'url': url_256}]}}
    assert extract_watch_providers(movie) == [{'name': 'Okko', 'url': url_256}]


@pytest.mark.parametrize('movie', [
    {},                                       # watchability отсутствует
    {'watchability': None},                   # None
    {'watchability': 'строка'},               # не словарь
    {'watchability': {}},                     # items отсутствует
    {'watchability': {'items': None}},        # items = None
    {'watchability': {'items': 'не-список'}}, # items не список
    {'watchability': {'items': []}},          # пустой items (штатно по разведке)
])
def test_watch_providers_degrades_to_empty(movie: Dict[str, Any]):
    """Любая патология — пустой список без исключений (graceful-деградация)."""
    assert extract_watch_providers(movie) == []


# --- 3. formatted_movie движка содержит новые поля ---


def _format_one(raw_movie: Dict[str, Any]) -> Dict[str, Any]:
    """Прогон сырого документа API через _format_movies_list (паттерн test_recommendation_engine)."""
    engine = RecommendationEngine(KinopoiskClient('test-kinopoisk-key'))
    result = engine._format_movies_list([raw_movie], limit=5)
    assert len(result) == 1
    return result[0]


def _raw_api_movie(**extra: Any) -> Dict[str, Any]:
    raw: Dict[str, Any] = {
        'id': 535341,
        'name': '1+1',
        'year': 2011,
        'genres': [{'name': 'драма'}],
        'countries': [{'name': 'Франция'}],
        'rating': {'imdb': 8.5, 'kp': 8.8},
        'votes': {'imdb': 100000, 'kp': 500000},
        'description': 'Описание',
        'poster': {'url': 'https://example.com/poster.jpg'},
        'type': 'movie',
    }
    raw.update(extra)
    return raw


def test_formatted_movie_contains_trailer_and_providers():
    formatted = _format_one(_raw_api_movie(
        videos={'trailers': [{'url': TRAILER_URL, 'site': 'youtube'}]},
        watchability={'items': _six_providers()},
    ))
    assert formatted['trailer_url'] == TRAILER_URL
    assert formatted['watch_providers'] == _six_providers()[:WATCH_PROVIDERS_LIMIT]


def test_formatted_movie_without_media_fields_degrades():
    """Нет videos/watchability (сегодняшняя норма API) — None и пустой список."""
    formatted = _format_one(_raw_api_movie())
    assert formatted['trailer_url'] is None
    assert formatted['watch_providers'] == []


# --- 4. Клавиатура карточки: кнопка трейлера и ряд провайдеров ---


def _rows_snapshot(markup: Any) -> List[List[Any]]:
    """Побайтовый снимок клавиатуры: (текст, url, callback_data) по рядам."""
    return [[(b.text, b.url, b.callback_data) for b in row] for row in markup.inline_keyboard]


def test_keyboard_without_media_is_identical_to_legacy():
    """Без trailer_url/watch_providers клавиатура побайтово прежняя (A4+B4+B5)."""
    legacy = build_movie_card_keyboard(make_movie())
    with_empty_fields = build_movie_card_keyboard(
        make_movie(trailer_url=None, watch_providers=[])
    )
    assert _rows_snapshot(with_empty_fields) == _rows_snapshot(legacy)
    # Состав рядов: два основных ряда по 2 кнопки + ряд фидбека
    assert _rows_snapshot(legacy) == [
        [(CARD_LINK_BUTTON_TEXT, 'https://www.kinopoisk.ru/film/435/', None),
         (CARD_SIMILAR_BUTTON_TEXT, None, 'similar:435')],
        [(CARD_SAVE_BUTTON_TEXT, None, 'save:435'),
         (CARD_BACK_BUTTON_TEXT, None, 'back:list')],
        [(FEEDBACK_RATE_BUTTON_TEXT, None, 'fb:rate:435'),
         (FEEDBACK_WATCHED_BUTTON_TEXT, None, 'fb:watched:435'),
         (FEEDBACK_NOPE_BUTTON_TEXT, None, 'fb:nope:435')],
    ]


def test_keyboard_trailer_button_right_after_kinopoisk_link():
    """Кнопка «▶️ Трейлер» — url, сразу после «🔗 Кинопоиск» (D4)."""
    markup = build_movie_card_keyboard(make_movie(trailer_url=TRAILER_URL))
    first_row = markup.inline_keyboard[0]
    assert [b.text for b in first_row] == [CARD_LINK_BUTTON_TEXT, CARD_TRAILER_BUTTON_TEXT]
    assert first_row[1].url == TRAILER_URL


@pytest.mark.parametrize('trailer_url', [None, '', 'ftp://example.com/x', 'www.youtube.com/x'])
def test_keyboard_no_trailer_button_on_invalid_url(trailer_url: Optional[str]):
    """Невалидный/пустой trailer_url — кнопки нет, раскладка прежняя."""
    markup = build_movie_card_keyboard(make_movie(trailer_url=trailer_url))
    assert CARD_TRAILER_BUTTON_TEXT not in [b.text for b in all_buttons(markup)]
    assert _rows_snapshot(markup) == _rows_snapshot(build_movie_card_keyboard(make_movie()))


def test_keyboard_providers_row_before_feedback_row():
    """Ряд провайдеров — после основных рядов и ПЕРЕД рядом фидбека (D4)."""
    markup = build_movie_card_keyboard(make_movie(
        watch_providers=[PROVIDER_OKKO, PROVIDER_KION, PROVIDER_IVI],
    ))
    rows = markup.inline_keyboard
    assert len(rows) == 4  # 2 основных + провайдеры + фидбек
    provider_row = rows[-2]
    assert [(b.text, b.url) for b in provider_row] == [
        ('Okko', PROVIDER_OKKO['url']),
        ('KION', PROVIDER_KION['url']),
        ('Иви', PROVIDER_IVI['url']),
    ]
    # Фидбек остался последним рядом (инвариант B5)
    assert [b.text for b in rows[-1]] == [
        FEEDBACK_RATE_BUTTON_TEXT, FEEDBACK_WATCHED_BUTTON_TEXT, FEEDBACK_NOPE_BUTTON_TEXT,
    ]


def test_keyboard_provider_buttons_have_no_callback_and_no_emoji():
    """Кнопки провайдеров — чистые url-кнопки без callback и без эмодзи."""
    markup = build_movie_card_keyboard(make_movie(watch_providers=[PROVIDER_OKKO]))
    provider_row = markup.inline_keyboard[-2]
    assert len(provider_row) == 1
    button = provider_row[0]
    assert button.callback_data is None
    assert button.url == PROVIDER_OKKO['url']
    assert button.text == 'Okko'  # имя бренда без эмодзи (гайдлайн, D6)


def test_keyboard_long_provider_name_truncated_to_64():
    """Длинное имя провайдера обрезается хелпером ≤64 с «…» в хвосте."""
    long_name = 'Стриминговый сервис с очень длинным названием' * 5
    markup = build_movie_card_keyboard(make_movie(
        watch_providers=[{'name': long_name, 'url': PROVIDER_OKKO['url']}],
    ))
    button = markup.inline_keyboard[-2][0]
    assert len(button.text) <= 64
    assert button.text.endswith('…')
    assert button.url == PROVIDER_OKKO['url']


def test_keyboard_ignores_invalid_provider_entries():
    """Патологические записи списка провайдеров не ломают клавиатуру."""
    markup = build_movie_card_keyboard(make_movie(watch_providers=[
        'не-словарь',
        {'name': '', 'url': 'https://x.ru'},
        {'name': 'БезHttp', 'url': 'ftp://x'},
    ]))
    assert _rows_snapshot(markup) == _rows_snapshot(build_movie_card_keyboard(make_movie()))


def test_keyboard_degrades_when_all_provider_urls_exceed_limit():
    """Все url длиннее лимита Bot API — ряда нет, клавиатура прежняя
    (деградация не ломается, minor ревью)."""
    long_url = 'https://okko.tv/movie/535341?' + 'u' * 300
    providers = extract_watch_providers({'watchability': {'items': [
        {'name': 'Okko', 'url': long_url},
        {'name': 'KION', 'url': long_url},
    ]}})
    assert providers == []
    markup = build_movie_card_keyboard(make_movie(watch_providers=providers))
    assert _rows_snapshot(markup) == _rows_snapshot(build_movie_card_keyboard(make_movie()))


def test_keyboard_trailer_and_providers_together():
    """Полный состав: трейлер во 2-й позиции, провайдеры перед фидбеком."""
    markup = build_movie_card_keyboard(make_movie(
        trailer_url=TRAILER_URL,
        watch_providers=[PROVIDER_OKKO, PROVIDER_KION],
    ))
    texts = [[b.text for b in row] for row in markup.inline_keyboard]
    assert texts == [
        [CARD_LINK_BUTTON_TEXT, CARD_TRAILER_BUTTON_TEXT],
        [CARD_SIMILAR_BUTTON_TEXT, CARD_SAVE_BUTTON_TEXT],
        [CARD_BACK_BUTTON_TEXT],
        ['Okko', 'KION'],
        [FEEDBACK_RATE_BUTTON_TEXT, FEEDBACK_WATCHED_BUTTON_TEXT, FEEDBACK_NOPE_BUTTON_TEXT],
    ]


# --- 5. Путь callback info:{id}: полный документ с watchability/videos ---


def test_handle_movie_detail_card_has_provider_and_trailer_buttons(monkeypatch: pytest.MonkeyPatch):
    """Полный документ /v1.4/movie/{id} — карточка с трейлером и провайдерами."""
    from conftest import KP_PAYLOAD

    payload = dict(KP_PAYLOAD)
    payload['videos'] = {'trailers': [{'url': TRAILER_URL, 'name': 'Трейлер', 'site': 'youtube'}]}
    payload['watchability'] = {'items': _six_providers()}

    dm = make_manager()
    install_callback_mocks(monkeypatch, dm, payload=payload)
    message = FakeMessage(chat=FakeChat())
    update = FakeCallbackUpdate('info:447301', message=message, from_user=FakeUser(777))
    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    assert len(message.photos) == 1
    markup = message.photos[0][1]['reply_markup']
    texts = [[b.text for b in row] for row in markup.inline_keyboard]
    # Трейлер — url-кнопка после «🔗 Кинопоиск», провайдеры — ряд перед фидбеком
    assert texts[0] == [CARD_LINK_BUTTON_TEXT, CARD_TRAILER_BUTTON_TEXT]
    assert texts[-2] == ['Okko', 'KION', 'Иви']
    assert texts[-1] == [FEEDBACK_RATE_BUTTON_TEXT, FEEDBACK_WATCHED_BUTTON_TEXT, FEEDBACK_NOPE_BUTTON_TEXT]
    urls = [b.url for b in all_buttons(markup) if b.url]
    assert TRAILER_URL in urls
    assert _six_providers()[0]['url'] in urls  # Okko из payload документа


def test_handle_movie_detail_without_media_keeps_legacy_keyboard(monkeypatch: pytest.MonkeyPatch):
    """Документ без videos/watchability (норма API сегодня) — клавиатура прежняя."""
    dm = make_manager()
    install_callback_mocks(monkeypatch, dm)  # KP_PAYLOAD без новых полей
    message = FakeMessage(chat=FakeChat())
    update = FakeCallbackUpdate('info:447301', message=message, from_user=FakeUser(777))
    asyncio.run(telegram_bot.handle_movie_detail(update, None))

    assert len(message.photos) == 1
    markup = message.photos[0][1]['reply_markup']
    assert CARD_TRAILER_BUTTON_TEXT not in [b.text for b in all_buttons(markup)]
    assert len(markup.inline_keyboard) == 3  # 2 основных ряда + фидбек


# --- 6. selectFields списковых запросов содержит videos и watchability ---


@pytest.mark.parametrize('method,kwargs', [
    ('search_movies', {}),
    ('search_recommendation', {}),
    ('search_movie_by_title', {'title': 'Начало'}),
])
def test_select_fields_contain_videos_and_watchability(method: str, kwargs: Dict[str, Any]):
    """Поля добавлены во все три запроса; /search их игнорирует — известно (D5)."""
    client = KinopoiskClient('test-kinopoisk-key')
    captured: Dict[str, Any] = {}

    async def _fake_request(session: Any, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        captured['params'] = params
        return {'docs': []}

    client._make_request = AsyncMock(side_effect=_fake_request)  # type: ignore[method-assign]
    result = asyncio.run(getattr(client, method)(None, **kwargs))
    assert result == {'docs': []}
    select_fields = captured['params']['selectFields']
    assert 'videos' in select_fields
    assert 'watchability' in select_fields
    # Регресс: существующие поля не потеряны
    assert 'externalId' in select_fields
    assert 'id' in select_fields
