# src/rt_cache.py
"""Кэш оценок Rotten Tomatoes/Metacritic в PostgreSQL (Epic B, задача B4).

Назначение — экономия дневной квоты OMDb (free-тариф: 1000 запросов/день):
оценки, полученные один раз, переиспользуются в течение TTL
(RT_CACHE_TTL_DAYS, дефолт 60 дней — середина рекомендуемого диапазона
30–90 из бэклога), а фильмы без оценок кэшируются отрицательно (обе
оценки NULL) и повторно не запрашиваются.

Контракты (согласованы с B1/B2):
- три исхода чтения: miss (нет записи/протухла/БД недоступна/флаг
  выключен), hit (есть хотя бы одна оценка), negative hit (запись есть,
  обе оценки NULL — в OMDb идти НЕ нужно); признак отрицательного кэша
  доступен свойством CacheLookup.is_negative;
- запись принимает результат источника целиком: set_scores(id, scores) —
  scores=None (сбой OMDb/флаг выключен) означает no-op, а
  RtScores(None, None) — отрицательный кэш. Перепутать эти два случая
  тип уже не даёт (см. design.md, риск «залипания» отрицательного кэша);
- fail-silent: любой сбой БД логируется (первый — warning, остальные —
  debug), сессия откатывается и исключение никогда не пробрасывается
  наружу — выдача рекомендаций не ломается;
- feature flag Epic B (config.rt_scores_enabled) проверяется в момент
  вызова: при выключенном флаге кэш не читается и не пишется, обращения
  к БД не происходит;
- асинхронные обёртки aget_scores/aset_scores выполняют блокирующие
  операции SQLAlchemy через asyncio.to_thread, чтобы не блокировать
  event loop бота (потребитель — задача B5); ленивое создание
  приложения кэша потокобезопасно (B5 читает кэш параллельно).

Доступ к БД из процесса бота — по паттерну statistics_tracker.py:
ленивое минимальное Flask-приложение с DATABASE_URL из env; веб и тесты
могут передать собственное приложение через параметр app.
"""
import asyncio
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from flask import Flask

from config import rt_scores_enabled
from omdb_client import RtScores

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = 'postgresql://postgres:postgres@localhost:5432/kinobot_db'

# TTL кэша по умолчанию (дни): середина рекомендуемого диапазона 30–90 (бэклог B4).
# Оценки RT меняются медленно, отрицательный кэш уместно держать долго.
DEFAULT_TTL_DAYS = 60

# Параметры пула соединений минимального приложения кэша. Задача B5 читает/пишет
# кэш параллельно (asyncio.gather по ~13 фильмам через asyncio.to_thread), поэтому
# пул ограничен явно: без этого SQLAlchemy возьмёт значения по умолчанию и может
# упереться в «QueuePool limit of size N overflow M reached». pool_pre_ping
# отсекает соединения, разорванные сервером (простой бота между выдачами).
# Применяется только к серверным диалектам: SQLite pool_size/max_overflow
# не поддерживает.
ENGINE_OPTIONS: Dict[str, Any] = {
    'pool_size': 5,
    'max_overflow': 10,
    'pool_pre_ping': True,
}


@dataclass(frozen=True, slots=True)
class CacheLookup:
    """Результат чтения кэша RT-оценок.

    hit=False — промах (miss): вызывающий код может обратиться к OMDb.
    hit=True — запись найдена и актуальна; scores=RtScores(None, None)
    означает отрицательный кэш: оценок у источника нет, повторный запрос
    к OMDb выполнять НЕ нужно (признак — свойство is_negative).

    Инвариант защищён типом: состояние hit=True при scores=None недопустимо
    (ValueError в __post_init__) — иначе потребитель (B5) получил бы
    AttributeError при обращении к lookup.scores.rt_score прямо в выдаче.
    """

    hit: bool
    scores: Optional[RtScores] = None

    def __post_init__(self) -> None:
        """Проверка инварианта: hit без оценок не существует."""
        if self.hit and self.scores is None:
            raise ValueError('hit=True требует непустой scores')

    @property
    def is_negative(self) -> bool:
        """Отрицательный кэш: запись есть, но оценок у источника нет."""
        return self.hit and self.scores is not None \
            and self.scores.rt_score is None and self.scores.metascore is None


# Единственный экземпляр промаха: чтение без записи/при сбое/при
# выключенном флаге всегда возвращает его.
MISS = CacheLookup(hit=False)

_app: Optional[Flask] = None
# Защита ленивой инициализации _app: B5 будет вызывать кэш параллельно
# (asyncio.gather -> asyncio.to_thread), без блокировки первый всплеск создал бы
# несколько приложений и, соответственно, несколько пулов соединений к PostgreSQL.
_app_lock = threading.Lock()
_db_warned = False


def _build_app() -> Flask:
    """Собрать минимальное Flask-приложение для доступа к БД (режим бота).

    Повторяет паттерн statistics_tracker.py: DATABASE_URL из env,
    TRACK_MODIFICATIONS выключен; дополнительно задаёт ограниченный пул
    соединений (ENGINE_OPTIONS) под параллельное обогащение в B5.
    """
    from models.database import db

    new_app = Flask('kinobot_rt_cache')
    uri = os.getenv('DATABASE_URL', DEFAULT_DATABASE_URL)
    new_app.config['SQLALCHEMY_DATABASE_URI'] = uri
    new_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    if uri.startswith('postgres'):
        new_app.config['SQLALCHEMY_ENGINE_OPTIONS'] = dict(ENGINE_OPTIONS)
    db.init_app(new_app)
    return new_app


def _get_app() -> Flask:
    """Единственное приложение кэша: ленивое создание под блокировкой.

    Double-checked locking: без блокировки читаем быстрый путь, под блокировкой
    проверяем повторно (другой поток мог успеть создать приложение первым).
    """
    global _app
    if _app is None:
        with _app_lock:
            if _app is None:
                _app = _build_app()
    return _app


def _db_failure(message: str, exc: Exception) -> None:
    """Fail-silent логирование сбоя БД: warning один раз, далее debug.

    По образцу statistics_tracker._db_warned: повторяющиеся сбои
    (PostgreSQL лежит) не должны заспамить лог на каждой выдаче.
    """
    global _db_warned
    if not _db_warned:
        logger.warning(f"{message}: {exc} — кэш RT-оценок считается недоступным")
        _db_warned = True
    else:
        logger.debug(f"{message}: {exc}")


def _ttl_days() -> int:
    """TTL кэша в днях из RT_CACHE_TTL_DAYS (читается в момент вызова).

    Чтение в момент вызова (а не на импорте модуля) консистентно с
    config.rt_scores_enabled и позволяет тестам менять TTL через
    monkeypatch.setenv. Некорректное значение (нечисло, < 1) —> дефолт;
    прочие значения применяются как есть (диапазон 30–90 — рекомендация,
    а не ограничение: клампинга нет, см. .env.example и design.md D5).
    """
    raw = os.getenv('RT_CACHE_TTL_DAYS')
    if raw is None:
        return DEFAULT_TTL_DAYS
    try:
        value = int(raw.strip())
    except ValueError:
        logger.debug(f"RT_CACHE_TTL_DAYS={raw!r} не является числом — использую дефолт {DEFAULT_TTL_DAYS}")
        return DEFAULT_TTL_DAYS
    if value < 1:
        logger.debug(f"RT_CACHE_TTL_DAYS={value} меньше 1 — использую дефолт {DEFAULT_TTL_DAYS}")
        return DEFAULT_TTL_DAYS
    return value


def _as_utc(moment: datetime) -> datetime:
    """Нормализация момента времени к aware-UTC для сравнения с TTL.

    SQLite (тесты) возвращает из БД naive-datetime, PostgreSQL
    (TIMESTAMPTZ) — aware. Мы пишем только aware-UTC, поэтому naive
    трактуем как UTC; в противном случае приводим к UTC.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def get_scores(imdb_id: Optional[str], app: Optional[Flask] = None) -> CacheLookup:
    """Прочитать оценки фильма из кэша по IMDb ID.

    Возвращает MISS (hit=False) при: выключенном feature flag Epic B,
    пустом imdb_id, отсутствии строки, протухшей записи (старше
    RT_CACHE_TTL_DAYS) и любом сбое БД. При актуальной записи —
    hit=True и scores (RtScores(None, None) для отрицательного кэша).
    Исключения никогда не пробрасываются (fail-silent).
    """
    if not rt_scores_enabled():
        # Флаг выключен — Epic B не работает вовсе: не трогаем даже БД
        return MISS
    if not imdb_id or not str(imdb_id).strip():
        return MISS
    key = str(imdb_id).strip()
    try:
        from models.database import RtScore, db
        with (app or _get_app()).app_context():
            row = db.session.get(RtScore, key)
            if row is None or row.fetched_at is None:
                # Нет записи (или повреждённая строка без момента получения)
                return MISS
            expires_before = datetime.now(timezone.utc) - timedelta(days=_ttl_days())
            if _as_utc(row.fetched_at) < expires_before:
                # Запись протухла по TTL — трактуем как miss (включая negative)
                return MISS
            return CacheLookup(
                hit=True,
                scores=RtScores(rt_score=row.rt_score, metascore=row.metascore),
            )
    except Exception as exc:
        _db_failure(f"Не удалось прочитать кэш RT-оценок для {key}", exc)
        return MISS


def set_scores(
    imdb_id: Optional[str],
    scores: Optional[RtScores],
    app: Optional[Flask] = None,
) -> None:
    """Записать (upsert) оценки фильма в кэш по IMDb ID.

    Контракт аргумента scores (защищает от «залипания» отрицательного кэша):
    - scores is None — источник не ответил (сбой fetch_rt_scores) либо feature
      flag выключен: запись НЕ выполняется вовсе (no-op), строка не создаётся;
    - scores = RtScores(None, None) — источник ответил, но оценок нет:
      сохраняется отрицательный кэш (обе колонки NULL), повторный запрос
      к OMDb не выполняется до истечения TTL;
    - scores с хотя бы одной оценкой — обычная запись.

    Создаёт строку при отсутствии и обновляет rt_score/metascore/fetched_at
    при наличии. При выключенном feature flag и при любом сбое БД — no-op
    (fail-silent, сессия откатывается).
    """
    if scores is None:
        # Сбой источника или выключенный флаг: кэшировать нечего
        return
    if not rt_scores_enabled():
        return
    if not imdb_id or not str(imdb_id).strip():
        return
    key = str(imdb_id).strip()
    try:
        from models.database import RtScore, db
        with (app or _get_app()).app_context():
            try:
                row = db.session.get(RtScore, key)
                now = datetime.now(timezone.utc)
                if row is None:
                    row = RtScore(
                        imdb_id=key,
                        rt_score=scores.rt_score,
                        metascore=scores.metascore,
                        fetched_at=now,
                    )
                    db.session.add(row)
                else:
                    row.rt_score = scores.rt_score
                    row.metascore = scores.metascore
                    row.fetched_at = now
                db.session.commit()
            except Exception:
                # Явный откат сессии в том же app-контексте: не полагаемся на
                # teardown Flask-SQLAlchemy (поведение зависит от версии FSA/
                # Flask) и не оставляем «грязную» сессию пулу соединений.
                # Гонка двух одновременных вставок (IntegrityError) гасится
                # здесь же: данные уже записал другой поток, повтор не нужен.
                try:
                    db.session.rollback()
                except Exception as rollback_exc:
                    logger.debug(f"rollback сессии кэша RT-оценок не выполнен: {rollback_exc}")
                raise
    except Exception as exc:
        _db_failure(f"Не удалось записать кэш RT-оценок для {key}", exc)


async def aget_scores(imdb_id: Optional[str], app: Optional[Flask] = None) -> CacheLookup:
    """Асинхронная обёртка get_scores: не блокирует event loop.

    Синхронный SQLAlchemy уводится в пул потоков стандартной библиотеки
    (asyncio.to_thread) — новых зависимостей не требует (design.md D6).
    """
    return await asyncio.to_thread(get_scores, imdb_id, app)


async def aset_scores(
    imdb_id: Optional[str],
    scores: Optional[RtScores],
    app: Optional[Flask] = None,
) -> None:
    """Асинхронная обёртка set_scores: не блокирует event loop.

    Контракт scores тот же, что у set_scores: None — no-op (сбой источника),
    RtScores(None, None) — отрицательный кэш.
    """
    await asyncio.to_thread(set_scores, imdb_id, scores, app)
