import time
from unittest.mock import MagicMock

from flask import Flask

import models.database as db_module
from session_manager import SessionManager


def test_get_session_does_not_write(monkeypatch):
    """Чтение сессии не выполняет коммит и не меняет строку в БД"""
    fake_row = MagicMock()
    fake_row.last_movies = [{'id': 1}]
    fake_row.last_params = {'genre': 'комедия'}
    fake_row.created_at = None

    query = MagicMock()
    query.filter_by.return_value.first.return_value = fake_row
    fake_model = MagicMock()
    fake_model.query = query

    fake_db = MagicMock()
    monkeypatch.setattr(db_module, 'DialogueSession', fake_model)
    monkeypatch.setattr(db_module, 'db', fake_db)

    manager = SessionManager(app=Flask('test'))
    manager._last_cleanup = time.time()

    first = manager.get_session('u1')
    second = manager.get_session('u1')

    fake_db.session.commit.assert_not_called()
    fake_db.session.add.assert_not_called()
    assert query.filter_by.call_count == 2
    assert first.last_movies == [{'id': 1}]
    assert second.last_params == {'genre': 'комедия'}
