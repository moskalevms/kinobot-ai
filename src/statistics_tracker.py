# src/statistics_tracker.py
"""Запись клиентских запросов в статистику.

Общий код для веба и Telegram-бота: сбой записи не должен прерывать
диалог пользователя, поэтому ошибки перехватываются и логируются.
"""
import os
import logging

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = 'postgresql://postgres:postgres@localhost:5432/kinobot_db'

_app = None
_db_warned = False


def _get_app():
    """Минимальное Flask-приложение для доступа к БД (режим бота)"""
    global _app
    if _app is None:
        from flask import Flask
        from models.database import db
        _app = Flask('kinobot_statistics')
        _app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
            'DATABASE_URL', DEFAULT_DATABASE_URL
        )
        _app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(_app)
    return _app


def track_client_request(session_id, user_agent=None, ip_address=None, app=None):
    """Записать клиентский запрос в статистику.

    Никогда не кидает исключение: при недоступности БД логирует
    предупреждение (один раз) и пропускает запись — диалог важнее
    статистики. Веб передаёт своё приложение через `app`, бот
    использует минимальное.
    """
    global _db_warned
    try:
        from models.database import UserStatistics
        with (app or _get_app()).app_context():
            UserStatistics.track_user(session_id, user_agent, ip_address)
    except Exception as e:
        if not _db_warned:
            logger.warning(f"Не удалось записать статистику: {e} — запись пропускается")
            _db_warned = True
        else:
            logger.debug(f"Не удалось записать статистику: {e}")
