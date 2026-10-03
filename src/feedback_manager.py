# src/feedback_manager.py
"""Доступ к хранилищу обратной связи по фильмам (Epic B, задача B5).

Бот — не Flask-приложение, поэтому доступ к PostgreSQL организован по
сложившемуся паттерну «ленивое минимальное Flask-приложение»
(session_manager.py, rt_cache.py, watchlist_manager.py): собственное
приложение `kinobot_feedback` с DATABASE_URL из окружения, все операции
внутри `with self._app.app_context():`.

Хранилище — таблица `movie_feedback` (модель
`models.database.MovieFeedback`, единственный источник схемы): ОДНА
строка на пару (user_id, kinopoisk_id) с независимыми полями reaction
('watched'/'nope') и rating (1–10). Запись — upsert: повторный тап той
же кнопки, смена оценки/реакции ОБНОВЛЯЮТ строку, а не плодят дубли
(критерий приёмки B5). Данные переживают рестарт бота и доступны любому
воркеру — in-memory фолбэка НЕТ осознанно (риск №4 бэклога: фидбек —
основа будущей персонализации C5, терять его нельзя). При недоступности
БД все методы fail-silent: пишут warning в лог (однократно) и возвращают
признак невыполнения — обработчик бота не получает исключение и честно
показывает ошибку с повтором (B7), а не ложное «✅ Сохранено».

Ранжирование рекомендаций в B5 НЕ меняется: менеджер только копит данные.
"""
import os
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = 'postgresql://postgres:postgres@localhost:5432/kinobot_db'


class FeedbackManager:
    """Операции с реакциями и оценками пользователя в PostgreSQL."""

    # Закрытый набор реакций: сегмент действия callback'а (fb:watched /
    # fb:nope) СОВПАДАЕТ со значением колонки reaction — единый литерал
    # для кнопки, маршрута и записи (design.md D3 изменения).
    REACTION_WATCHED = 'watched'
    REACTION_NOPE = 'nope'
    REACTIONS = frozenset({REACTION_WATCHED, REACTION_NOPE})
    # Шкала оценки (кнопки панели 1–10, CHECK-ограничение схемы).
    RATING_MIN = 1
    RATING_MAX = 10

    def __init__(self, app: Any = None) -> None:
        # Флаг однократного warning о недоступности БД (образец —
        # WatchlistManager._db_warned): повторяющиеся сбои не заспамляют лог.
        self._db_warned = False
        self._app = app if app is not None else self._create_minimal_app()

    def _create_minimal_app(self) -> Any:
        """Минимальное Flask-приложение для доступа к БД (режим бота).

        Копия паттерна WatchlistManager._create_minimal_app: DATABASE_URL
        из env, TRACK_MODIFICATIONS выключен, модели регистрируются общим
        расширением db.
        """
        from flask import Flask
        from models.database import db
        app = Flask('kinobot_feedback')
        app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
            'DATABASE_URL', DEFAULT_DATABASE_URL
        )
        app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(app)
        return app

    def _db_error(self, message: str, e: Exception) -> None:
        """Fail-silent логирование сбоя БД: warning один раз, далее debug."""
        if not self._db_warned:
            logger.warning(f"{message}: {e} — хранилище фидбека считается недоступным")
            self._db_warned = True
        else:
            logger.debug(f"{message}: {e}")

    def set_reaction(self, user_id: str, kinopoisk_id: int, reaction: str) -> bool:
        """Записать реакцию («watched»/«nope») — upsert одной строки пары.

        True — строка создана или обновлена; False — некорректное значение
        либо БД недоступна. Повторная та же реакция идемпотентна, смена
        реакции перезаписывает прежнюю (вторая строка не создаётся —
        уникальность (user_id, kinopoisk_id) гарантирует схема).
        """
        if reaction not in self.REACTIONS:
            logger.warning(f"Неизвестная реакция фидбека: {reaction!r}")
            return False
        return self._upsert(user_id, kinopoisk_id, reaction=reaction)

    def set_rating(self, user_id: str, kinopoisk_id: int, rating: int) -> bool:
        """Записать оценку 1–10 — upsert одной строки пары.

        True — строка создана или обновлена; False — оценка вне диапазона
        (защита от мусорных callback'ов, до БД не доходит) либо БД
        недоступна. Повторная оценка перезаписывает прежнюю.
        """
        if isinstance(rating, bool) or not isinstance(rating, int) or not (self.RATING_MIN <= rating <= self.RATING_MAX):
            logger.warning(f"Некорректная оценка фидбека: {rating!r}")
            return False
        return self._upsert(user_id, kinopoisk_id, rating=rating)

    def clear_rating(self, user_id: str, kinopoisk_id: int) -> bool:
        """Сбросить оценку (rating=NULL), сохранив реакцию строки.

        True — оценки больше нет (обнулена либо строки не было: нечего
        сбрасывать — штатная ситуация, а не ошибка, design.md D5/Risks);
        False — БД недоступна.
        """
        from models.database import db, MovieFeedback
        try:
            with self._app.app_context():
                row = db.session.query(MovieFeedback).filter_by(
                    user_id=user_id, kinopoisk_id=kinopoisk_id
                ).first()
                if row is not None:
                    row.rating = None
                    db.session.commit()
            return True
        except Exception as e:
            self._db_error("Не удалось сбросить оценку фильма", e)
            return False

    def _upsert(self, user_id: str, kinopoisk_id: int, **fields: Any) -> bool:
        """Переносимый upsert: select-then-update/insert + повтор при гонке.

        Диалект-специфичный `INSERT … ON CONFLICT` (postgresql) НЕ
        используется осознанно (design.md D2.1): тесты работают на
        in-memory SQLite, а единственный переносимый путь одинаково
        исполняется в обоих диалектах. Гонка двух одновременных тапов
        ловится перехватом IntegrityError (уникальность пары гарантирует
        схема): сессия откатывается и операция повторяется как UPDATE —
        вторая строка не появляется ни при каком порядке вызовов.
        """
        from sqlalchemy.exc import IntegrityError
        from models.database import db, MovieFeedback
        try:
            with self._app.app_context():
                self._merge_fields(db, MovieFeedback, user_id, kinopoisk_id, fields)
            return True
        except IntegrityError:
            # Строку уже создал параллельный insert — откат и повтор как
            # UPDATE (в новом контексте сессия чистая, rollback защитный)
            try:
                with self._app.app_context():
                    db.session.rollback()
                    self._merge_fields(db, MovieFeedback, user_id, kinopoisk_id, fields)
                return True
            except Exception as retry_err:
                self._db_error("Повтор upsert-а фидбека после гонки не удался", retry_err)
                return False
        except Exception as e:
            self._db_error("Не удалось сохранить фидбек фильма", e)
            return False

    @staticmethod
    def _merge_fields(db: Any, model: Any, user_id: str, kinopoisk_id: int, fields: Dict[str, Any]) -> None:
        """Обновить поля существующей строки пары либо вставить новую."""
        row = db.session.query(model).filter_by(
            user_id=user_id, kinopoisk_id=kinopoisk_id
        ).first()
        if row is None:
            row = model(user_id=user_id, kinopoisk_id=kinopoisk_id, **fields)
            db.session.add(row)
        else:
            for name, value in fields.items():
                setattr(row, name, value)
        db.session.commit()

    def get_feedback(self, user_id: str, kinopoisk_id: int) -> Optional[Dict[str, Any]]:
        """Фидбек пользователя по одному фильму; None — нет данных либо сбой БД.

        Словарь-копия полей детачирован от SQLAlchemy-сессии и безопасен
        для использования из любого потока после asyncio.to_thread.
        """
        from models.database import MovieFeedback
        try:
            with self._app.app_context():
                row = MovieFeedback.query.filter_by(
                    user_id=user_id, kinopoisk_id=kinopoisk_id
                ).first()
                return self._to_dict(row) if row is not None else None
        except Exception as e:
            self._db_error("Не удалось прочитать фидбек фильма", e)
            return None

    def list_user_feedback(self, user_id: str) -> List[Dict[str, Any]]:
        """Весь фидбек пользователя (основа будущей персонализации C5).

        Сортировка — по updated_at DESC (сначала свежие изменения);
        записи других пользователей в результат не попадают. Сбой БД — [].
        """
        from models.database import MovieFeedback
        try:
            with self._app.app_context():
                rows = MovieFeedback.query.filter_by(user_id=user_id).order_by(
                    MovieFeedback.updated_at.desc()
                ).all()
                return [self._to_dict(row) for row in rows]
        except Exception as e:
            self._db_error("Не удалось прочитать список фидбека пользователя", e)
            return []

    @staticmethod
    def _to_dict(row: Any) -> Dict[str, Any]:
        """Словарь-копия строки фидбека (детачированный от сессии результат)."""
        return {
            'kinopoisk_id': row.kinopoisk_id,
            'reaction': row.reaction,
            'rating': row.rating,
            'created_at': row.created_at,
            'updated_at': row.updated_at,
        }


# Ленивый синглтон модуля (образец — watchlist_manager): единственное
# Flask-приложение фидбека на процесс создаётся при первом обращении.
_manager: Optional[FeedbackManager] = None


def get_feedback_manager() -> FeedbackManager:
    """Единственный FeedbackManager процесса (ленивая инициализация)."""
    global _manager
    if _manager is None:
        _manager = FeedbackManager()
    return _manager
