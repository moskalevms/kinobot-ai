# src/session_manager.py
import time
import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


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

    def to_dict(self) -> Dict[str, Any]:
        return {
            'user_id': self.user_id,
            'last_movies': self.last_movies,
            'last_params': self.last_params,
            'dialogue_history': self.dialogue_history,
            'created_at': self.created_at,
            'last_activity': self.last_activity
        }


class SessionManager:
    def __init__(self, session_timeout: int = 3600):  # 1 час
        self.sessions: Dict[str, UserSession] = {}
        self.session_timeout = session_timeout

    def get_session(self, user_id: str) -> UserSession:
        """Получить сессию пользователя, создать если не существует"""
        if user_id not in self.sessions:
            self.sessions[user_id] = UserSession(user_id)
            logger.info(f"Создана новая сессия для пользователя {user_id}")
        else:
            self.sessions[user_id].update_activity()
        return self.sessions[user_id]

    def update_session(self, user_id: str, **kwargs):
        """Обновить данные сессии"""
        session = self.get_session(user_id)
        for key, value in kwargs.items():
            if hasattr(session, key):
                setattr(session, key, value)
        session.update_activity()

    def clear_session(self, user_id: str):
        """Очистить сессию пользователя"""
        if user_id in self.sessions:
            del self.sessions[user_id]
            logger.info(f"Сессия пользователя {user_id} очищена")

    def cleanup_expired_sessions(self):
        """Очистить просроченные сессии"""
        current_time = time.time()
        expired_users = []

        for user_id, session in self.sessions.items():
            if current_time - session.last_activity > self.session_timeout:
                expired_users.append(user_id)

        for user_id in expired_users:
            del self.sessions[user_id]

        if expired_users:
            logger.info(f"Очищено {len(expired_users)} просроченных сессий")