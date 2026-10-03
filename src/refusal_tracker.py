# src/refusal_tracker.py
"""Запись метрик офтопик-отказов guardrails в базу данных (Epic B, задача B7).

Общий код для веба и Telegram-бота: сбой записи НЕ должен прерывать
диалог пользователя (отказ уже сформирован), поэтому ошибки
перехватываются и логируются — паттерн скопирован со
statistics_tracker.track_client_request (fail-silent, warning один раз).

Приватность: в БД сохраняется только усечённый фрагмент сообщения
(≤120 символов — та же граница, что в guardrails.log_blocked), полный
ввод и системный промпт в хранилище не попадают.

guardrails.py НАМЕРЕННО не меняется: он остаётся чистой функцией без
зависимости от БД/Flask; запись в БД выполняется отдельным вызовом
рядом с log_blocked в dialogue_manager.process_message.
"""
import os
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = 'postgresql://postgres:postgres@localhost:5432/kinobot_db'

_app: Optional[Any] = None
_db_warned = False


def _get_app() -> Any:
    """Минимальное Flask-приложение для доступа к БД (режим бота).

    Копия паттерна statistics_tracker._get_app: ленивое собственное
    приложение с DATABASE_URL из окружения, модели регистрируются общим
    расширением db.
    """
    global _app
    if _app is None:
        from flask import Flask
        from models.database import db
        _app = Flask('kinobot_refusals')
        _app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
            'DATABASE_URL', DEFAULT_DATABASE_URL
        )
        _app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(_app)
    return _app


def record_refusal(user_id: str, reason: str, message: str, app: Optional[Any] = None) -> None:
    """Записать отказ guardrails в таблицу offtopic_refusals.

    Никогда не кидает исключение: при недоступности БД логирует
    предупреждение (один раз через флаг _db_warned, дальше debug) и
    пропускает запись — отказ пользователю важнее метрики. Веб передаёт
    своё приложение через `app`, бот — приложение своего менеджера
    сессий либо минимальное (ленивое). Фрагмент обрезается до
    OfftopicRefusal.FRAGMENT_LIMIT (120) символов ДО записи.
    """
    global _db_warned
    try:
        from models.database import db, OfftopicRefusal
        fragment = (message or '')[:OfftopicRefusal.FRAGMENT_LIMIT]
        with (app or _get_app()).app_context():
            refusal = OfftopicRefusal(
                user_id=str(user_id),
                reason=reason,
                message_fragment=fragment,
            )
            db.session.add(refusal)
            db.session.commit()
    except Exception as e:
        if not _db_warned:
            logger.warning(f"Не удалось записать метрику отказа: {e} — запись пропускается")
            _db_warned = True
        else:
            logger.debug(f"Не удалось записать метрику отказа: {e}")
