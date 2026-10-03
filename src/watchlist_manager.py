# src/watchlist_manager.py
"""Доступ к списку сохранённых фильмов «📌 Мой список» (Epic B, задача B4).

Бот — не Flask-приложение, поэтому доступ к PostgreSQL организован по
сложившемуся паттерну «ленивое минимальное Flask-приложение»
(session_manager.py, rt_cache.py, statistics_tracker.py): собственное
приложение `kinobot_watchlist` с DATABASE_URL из окружения, все операции
внутри `with self._app.app_context():`.

Хранилище — таблица `watchlist` (модель `models.database.Watchlist`,
единственный источник схемы). Сохранённое переживает рестарт бота и
доступно любому воркеру — in-memory фолбэка НЕТ осознанно (критерий
приёмки B4 — персистентность; «список-призрак» вводил бы пользователя в
заблуждение). При недоступности БД все методы fail-silent: пишут warning
в лог (однократно) и возвращают безопасное значение — обработчик бота не
получает исключение и отвечает пользователю дружелюбно (B7).
"""
import os
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = 'postgresql://postgres:postgres@localhost:5432/kinobot_db'


class WatchlistManager:
    """Операции со списком сохранённых фильмов пользователя в PostgreSQL."""

    def __init__(self, app: Any = None) -> None:
        # Флаг однократного warning о недоступности БД (образец —
        # SessionManager._db_warned): повторяющиеся сбои не заспамляют лог.
        self._db_warned = False
        self._app = app if app is not None else self._create_minimal_app()

    def _create_minimal_app(self) -> Any:
        """Минимальное Flask-приложение для доступа к БД (режим бота).

        Копия паттерна SessionManager._create_minimal_app: DATABASE_URL из
        env, TRACK_MODIFICATIONS выключен, модели регистрируются общим
        расширением db.
        """
        from flask import Flask
        from models.database import db
        app = Flask('kinobot_watchlist')
        app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
            'DATABASE_URL', DEFAULT_DATABASE_URL
        )
        app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(app)
        return app

    def _db_error(self, message: str, e: Exception) -> None:
        """Fail-silent логирование сбоя БД: warning один раз, далее debug."""
        if not self._db_warned:
            logger.warning(f"{message}: {e} — watchlist считается недоступным")
            self._db_warned = True
        else:
            logger.debug(f"{message}: {e}")

    def add(
        self,
        user_id: str,
        *,
        kinopoisk_id: int,
        title: str,
        year: Optional[int],
        poster_url: Optional[str],
    ) -> bool:
        """Сохранить фильм в список пользователя (идемпотентно).

        True — запись создана; False — фильм уже в списке (дубль) либо БД
        недоступна. Идемпотентность гарантируется ограничением уникальности
        (user_id, kinopoisk_id) на уровне БД: гонка двух одновременных
        тапов ловится перехватом IntegrityError (rollback оставляет сессию
        чистой), вторая запись не появляется ни при каком порядке вызовов.
        """
        from sqlalchemy.exc import IntegrityError
        from models.database import db, Watchlist
        try:
            with self._app.app_context():
                row = Watchlist(
                    user_id=user_id,
                    kinopoisk_id=kinopoisk_id,
                    title=title,
                    year=year,
                    poster_url=poster_url,
                )
                db.session.add(row)
                db.session.commit()
            return True
        except IntegrityError:
            # Дубль (уникальность (user_id, kinopoisk_id)) — штатная
            # ситуация: откатываем сессию и сообщаем «уже в списке»
            try:
                with self._app.app_context():
                    db.session.rollback()
            except Exception as rollback_err:  # pragma: no cover - защита
                logger.debug(f"Откат сессии после IntegrityError не удался: {rollback_err}")
            return False
        except Exception as e:
            self._db_error("Не удалось сохранить фильм в watchlist", e)
            return False

    def remove(self, user_id: str, kinopoisk_id: int) -> bool:
        """Удалить фильм из списка пользователя.

        True — строка удалена; False — записи не было (устаревшая кнопка,
        штатная ситуация) либо БД недоступна.
        """
        from models.database import db, Watchlist
        try:
            with self._app.app_context():
                deleted = Watchlist.query.filter_by(
                    user_id=user_id, kinopoisk_id=kinopoisk_id
                ).delete()
                db.session.commit()
            return bool(deleted)
        except Exception as e:
            self._db_error("Не удалось удалить фильм из watchlist", e)
            return False

    def contains(self, user_id: str, kinopoisk_id: int) -> bool:
        """True, если фильм уже в списке пользователя (БД недоступна — False)."""
        from models.database import Watchlist
        try:
            with self._app.app_context():
                return Watchlist.query.filter_by(
                    user_id=user_id, kinopoisk_id=kinopoisk_id
                ).first() is not None
        except Exception as e:
            self._db_error("Не удалось проверить наличие фильма в watchlist", e)
            return False

    def list_page(self, user_id: str, offset: int, limit: int) -> Tuple[List[Dict[str, Any]], int]:
        """Страница списка пользователя: (элементы, общее количество).

        Сортировка — по added_at DESC (сначала свежие), срез LIMIT/OFFSET
        выполняется на стороне БД (список может расти). Элементы —
        словари-копии полей строки: они детачированы от SQLAlchemy-сессии и
        безопасны для использования из любого потока после asyncio.to_thread.
        Отрицательный offset нормализуется в 0. Сбой БД — ([], 0).
        """
        from models.database import Watchlist
        try:
            safe_offset = max(0, offset)
            with self._app.app_context():
                query = Watchlist.query.filter_by(user_id=user_id)
                total = query.count()
                rows = query.order_by(Watchlist.added_at.desc()).offset(safe_offset).limit(limit).all()
                items = [
                    {
                        'kinopoisk_id': row.kinopoisk_id,
                        'title': row.title,
                        'year': row.year,
                        'poster_url': row.poster_url,
                        'added_at': row.added_at,
                    }
                    for row in rows
                ]
            return items, total
        except Exception as e:
            self._db_error("Не удалось прочитать watchlist", e)
            return [], 0


# Ленивый синглтон модуля (образец — statistics_tracker): единственное
# Flask-приложение watchlist на процесс создаётся при первом обращении.
_manager: Optional[WatchlistManager] = None


def get_watchlist_manager() -> WatchlistManager:
    """Единственный WatchlistManager процесса (ленивая инициализация)."""
    global _manager
    if _manager is None:
        _manager = WatchlistManager()
    return _manager
