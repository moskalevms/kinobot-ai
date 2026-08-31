# src/session_manager.py
import os
import time
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Как часто запускать очистку просроченных сессий (секунды)
CLEANUP_INTERVAL = 600

DEFAULT_DATABASE_URL = 'postgresql://postgres:postgres@localhost:5432/kinobot_db'


@dataclass
class UserSession:
    user_id: str
    last_movies: List[Dict] = field(default_factory=list)
    last_params: Dict[str, Any] = field(default_factory=dict)
    dialogue_history: List[Dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)

    def update_activity(self):
        self.last_activity = time.time()

    def to_dict(self) -> Dict:
        return {
            'user_id': self.user_id,
            'last_movies': self.last_movies,
            'last_params': self.last_params,
            'dialogue_history': self.dialogue_history,
            'created_at': self.created_at,
            'last_activity': self.last_activity
        }


class SessionManager:
    """Сессии диалога в PostgreSQL с in-memory фолбэком.

    Сессия хранится в БД, поэтому переживает рестарт процесса и доступна
    всем воркерам. Если БД недоступна — прозрачный переход на in-memory
    хранилище с предупреждением в лог: диалог не должен падать из-за
    хранилища сессий.
    """

    def __init__(self, session_timeout: int = 3600, app=None):
        self.session_timeout = session_timeout
        self._fallback_sessions: Dict[str, UserSession] = {}
        self._db_warned = False
        self._last_cleanup = 0.0
        self._app = app if app is not None else self._create_minimal_app()

    def _create_minimal_app(self):
        """Минимальное Flask-приложение для доступа к БД (режим бота)"""
        from flask import Flask
        from models.database import db
        app = Flask('kinobot_sessions')
        app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
            'DATABASE_URL', DEFAULT_DATABASE_URL
        )
        app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(app)
        return app

    def _db_error(self, message: str, e: Exception):
        if not self._db_warned:
            logger.warning(f"{message}: {e} — переключаюсь на in-memory сессии")
            self._db_warned = True
        else:
            logger.debug(f"{message}: {e}")

    def get_session(self, user_id: str) -> UserSession:
        """Получить сессию пользователя.

        Чтение не пишет в БД (ни коммита, ни обновления активности):
        строка сессии создаётся и обновляется только в save_session.
        Если записи ещё нет, возвращается новая сессия в памяти.
        """
        self._maybe_cleanup()
        try:
            from models.database import DialogueSession
            with self._app.app_context():
                row = DialogueSession.query.filter_by(user_id=user_id).first()
                if row is None:
                    return UserSession(user_id=user_id)
                return UserSession(
                    user_id=user_id,
                    last_movies=row.last_movies or [],
                    last_params=row.last_params or {},
                    created_at=row.created_at.timestamp() if row.created_at else time.time(),
                    last_activity=time.time(),
                )
        except Exception as e:
            self._db_error("БД недоступна при чтении сессии", e)
            return self._get_fallback_session(user_id)

    def _get_fallback_session(self, user_id: str) -> UserSession:
        if user_id not in self._fallback_sessions:
            self._fallback_sessions[user_id] = UserSession(user_id)
        else:
            self._fallback_sessions[user_id].update_activity()
        return self._fallback_sessions[user_id]

    def save_session(self, session: UserSession):
        """Сохранить изменения сессии"""
        session.update_activity()
        try:
            from models.database import db, DialogueSession
            with self._app.app_context():
                row = DialogueSession.query.filter_by(user_id=session.user_id).first()
                if row is None:
                    row = DialogueSession(user_id=session.user_id)
                    db.session.add(row)
                row.last_movies = session.last_movies
                row.last_params = session.last_params
                row.last_activity = datetime.utcnow()
                db.session.commit()
        except Exception as e:
            self._db_error("Не удалось сохранить сессию в БД", e)
            self._fallback_sessions[session.user_id] = session

    def update_session(self, user_id: str, **kwargs):
        """Обновить данные сессии"""
        session = self.get_session(user_id)
        for key, value in kwargs.items():
            if hasattr(session, key):
                setattr(session, key, value)
        self.save_session(session)

    def clear_session(self, user_id: str):
        """Очистить сессию пользователя"""
        self._fallback_sessions.pop(user_id, None)
        try:
            from models.database import db, DialogueSession
            with self._app.app_context():
                DialogueSession.query.filter_by(user_id=user_id).delete()
                db.session.commit()
            logger.info(f"Сессия пользователя {user_id} очищена")
        except Exception as e:
            self._db_error("Не удалось очистить сессию в БД", e)

    def count_sessions(self) -> int:
        """Количество активных сессий (для /health)"""
        try:
            from models.database import DialogueSession
            with self._app.app_context():
                return DialogueSession.query.count()
        except Exception:
            return len(self._fallback_sessions)

    def _maybe_cleanup(self):
        now = time.time()
        if now - self._last_cleanup >= CLEANUP_INTERVAL:
            self._last_cleanup = now
            self.cleanup_expired_sessions()

    def cleanup_expired_sessions(self):
        """Удалить просроченные сессии (БД + in-memory фолбэк)"""
        current_time = time.time()
        expired = [
            uid for uid, s in self._fallback_sessions.items()
            if current_time - s.last_activity > self.session_timeout
        ]
        for uid in expired:
            del self._fallback_sessions[uid]

        deleted = 0
        try:
            from models.database import db, DialogueSession
            cutoff = datetime.utcnow() - timedelta(seconds=self.session_timeout)
            with self._app.app_context():
                deleted = DialogueSession.query.filter(
                    DialogueSession.last_activity < cutoff
                ).delete(synchronize_session=False)
                db.session.commit()
        except Exception as e:
            self._db_error("Не удалось очистить просроченные сессии в БД", e)

        if deleted or expired:
            logger.info(
                f"Очищено просроченных сессий: {deleted} в БД, "
                f"{len(expired)} in-memory"
            )
