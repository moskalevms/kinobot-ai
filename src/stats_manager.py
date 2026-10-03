# src/stats_manager.py
"""Доступ к персональной статистике пользователя (Epic C, задача C5).

Бот — не Flask-приложение, поэтому доступ к PostgreSQL организован по
сложившемуся паттерну «ленивое минимальное Flask-приложение»
(session_manager.py, statistics_tracker.py, watchlist_manager.py,
feedback_manager.py): собственное приложение `kinobot_stats` с DATABASE_URL
из окружения, все операции внутри `with self._app.app_context():`.

Сводка собирается READ-ONLY из трёх существующих таблиц (единственный
источник схемы — models.database): `user_statistics` (обращения к боту,
N), `movie_feedback` (оценки и реакции, Y/средняя/watched/nope) и
`watchlist` (сохранённые фильмы, Z). Агрегации выполняются на стороне БД
(sum/count/avg + case), а не выборкой всех строк: объём данных
пользователя не ограничен, а счётчикам строки не нужны. Схема БД не
меняется — «любимый жанр» честно опущен (design.md D2 изменения
add-personal-stats-command: накопительного хранилища жанров нет).

При недоступности БД метод fail-silent: пишет warning в лог (однократно) и
возвращает безопасный словарь с `available=False` — обработчик бота не
получает исключение и показывает ЧЕСТНУЮ ошибку с повтором (B7), а не
вводящую в заблуждение заглушку «статистика пуста» (design.md D4).
Результат — словарь примитивов: он детачирован от SQLAlchemy-сессии и
безопасен для использования из любого потока после asyncio.to_thread.
"""
import os
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = 'postgresql://postgres:postgres@localhost:5432/kinobot_db'


class StatsManager:
    """Read-only агрегация персональной статистики пользователя из PostgreSQL."""

    # Префикс session_id клиента-бота в user_statistics: бот пишет строки
    # через track_client_request(f"tg:{user_id}") (telegram_bot.py), веб —
    # своими ключами. Литерал обязан совпадать с вызовами записи, иначе
    # числитель N молча обнулится (design.md D1/Risks).
    SESSION_PREFIX = 'tg:'

    def __init__(self, app: Any = None) -> None:
        # Флаг однократного warning о недоступности БД (образец —
        # FeedbackManager._db_warned): повторяющиеся сбои не заспамляют лог.
        self._db_warned = False
        self._app = app if app is not None else self._create_minimal_app()

    def _create_minimal_app(self) -> Any:
        """Минимальное Flask-приложение для доступа к БД (режим бота).

        Копия паттерна FeedbackManager._create_minimal_app: DATABASE_URL
        из env, TRACK_MODIFICATIONS выключен, модели регистрируются общим
        расширением db.
        """
        from flask import Flask
        from models.database import db
        app = Flask('kinobot_stats')
        app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
            'DATABASE_URL', DEFAULT_DATABASE_URL
        )
        app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(app)
        return app

    def _db_error(self, message: str, e: Exception) -> None:
        """Fail-silent логирование сбоя БД: warning один раз, далее debug."""
        if not self._db_warned:
            logger.warning(f"{message}: {e} — хранилище статистики считается недоступным")
            self._db_warned = True
        else:
            logger.debug(f"{message}: {e}")

    @staticmethod
    def _empty(available: bool) -> Dict[str, Any]:
        """Безопасный словарь-результат: нули и признак доступности данных."""
        return {
            'available': available,
            'queries': 0,
            'rated': 0,
            'avg_rating': None,
            'watched': 0,
            'nope': 0,
            'watchlist': 0,
        }

    def get_user_stats(self, user_id: str) -> Dict[str, Any]:
        """Персональная сводка пользователя: счётчики из трёх таблиц за один контекст.

        Поля результата: `available` (False — хранилище недоступно, нули
        НЕ означают «пусто»), `queries` — сумма queries_count по всем дням
        (N обращений к боту), `rated` — число фильмов с оценкой (Y),
        `avg_rating` — средняя оценка пользователя (1 знак, None если оценок
        нет), `watched`/`nope` — счётчики реакций фидбека, `watchlist` —
        число сохранённых фильмов (Z). Все три таблицы читаются в ОДНОМ
        app_context: сводка либо собирается целиком, либо не собирается
        вовсе (единый путь fail-silent, design.md D1).
        """
        from sqlalchemy import case, func
        from feedback_manager import FeedbackManager
        from models.database import db, MovieFeedback, UserStatistics, Watchlist
        try:
            session_id = f'{self.SESSION_PREFIX}{user_id}'
            with self._app.app_context():
                # N: user_statistics хранит по строке на (session_id, date) —
                # суммируем накопительный счётчик по всем дням.
                queries = db.session.query(
                    func.coalesce(func.sum(UserStatistics.queries_count), 0)
                ).filter(UserStatistics.session_id == session_id).scalar()
                # Y/средняя/реакции: одна агрегация по movie_feedback.
                # Значения реакций — константы FeedbackManager (единый
                # литерал кнопки, маршрута, записи и чтения, design B5 D3).
                rated, avg_rating, watched, nope = db.session.query(
                    func.count(MovieFeedback.rating),
                    func.avg(MovieFeedback.rating),
                    func.coalesce(func.sum(
                        case((MovieFeedback.reaction == FeedbackManager.REACTION_WATCHED, 1), else_=0)
                    ), 0),
                    func.coalesce(func.sum(
                        case((MovieFeedback.reaction == FeedbackManager.REACTION_NOPE, 1), else_=0)
                    ), 0),
                ).filter(MovieFeedback.user_id == user_id).one()
                # Z: число строк списка сохранённых фильмов.
                watchlist = db.session.query(
                    func.count(Watchlist.id)
                ).filter(Watchlist.user_id == user_id).scalar()
            stats = self._empty(available=True)
            stats['queries'] = int(queries or 0)
            stats['rated'] = int(rated or 0)
            # func.avg в PostgreSQL возвращает Decimal, в SQLite — float:
            # явный float() до округления делает результат переносимым (D3).
            stats['avg_rating'] = round(float(avg_rating), 1) if avg_rating is not None else None
            stats['watched'] = int(watched or 0)
            stats['nope'] = int(nope or 0)
            stats['watchlist'] = int(watchlist or 0)
            return stats
        except Exception as e:
            self._db_error("Не удалось прочитать статистику пользователя", e)
            return self._empty(available=False)


# Ленивый синглтон модуля (образец — feedback_manager): единственное
# Flask-приложение статистики на процесс создаётся при первом обращении.
_manager: Optional[StatsManager] = None


def get_stats_manager() -> StatsManager:
    """Единственный StatsManager процесса (ленивая инициализация)."""
    global _manager
    if _manager is None:
        _manager = StatsManager()
    return _manager
