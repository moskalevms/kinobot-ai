"""Тесты B7: метрики офтопик-отказов в админке (изменение add-offtopic-metrics-b7).

Имя файла с префиксом bf_ ОБЯЗАТЕЛЬНО: test_error_recovery_b7.py занят
задачей B7 СТАРОГО uiux-бэклога (образец — test_web_channel_bf_b5.py).

Покрывают:
- модель `OfftopicRefusal`: состав колонок (user_id/reason/
  message_fragment/created_at), nullable, индексы, таблица создаётся
  `db.create_all()` (in-memory SQLite);
- `refusal_tracker.record_refusal`: запись строки, обрезка фрагмента
  до ≤120 символов (приватность), fail-silent при недоступности БД
  (исключение не пробрасывается, warning один раз);
- `OfftopicRefusal.get_top_fragments`: группировка fragment+reason,
  count, last_seen=max(created_at), фильтр за период, сортировка
  count desc + детерминированный тай-брейк last_seen desc, limit;
- интеграция с `DialogueManager.process_message`: запись отказа в
  обеих ветках — precheck-блок и llm_offtopic (spy на record_refusal
  в пространстве имён dialogue_manager, LLM мокируется);
- конвенция init_db.py: db.create_all() есть, сырого CREATE TABLE нет,
  offtopic_refusals упомянута в перечне таблиц;
- админ-роут /admin/offtopic: аноним → редирект на login, админ →
  200 и фрагмент в теле; пользовательский фрагмент с HTML/JS
  рендерится ЭКРАНИРОВАННО (Jinja2 autoescape — регрессия XSS),
  закрывающие теги таблицы/страницы на месте (валидный HTML).

Все проверки офлайн: БД — in-memory SQLite, сеть и реальный Telegram
не используются.
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from flask import Flask
from flask_login import LoginManager
from sqlalchemy import inspect

import dialogue_manager as dm_module
import refusal_tracker
from conftest import make_manager, run_coro
from guardrails import REFUSAL_OFFTOPIC
from models.database import db, OfftopicRefusal, Role, User

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / 'src' / 'templates'


# === 1. Модель OfftopicRefusal и единственный источник схемы ===


def test_offtopic_refusal_model_schema():
    """Колонки, nullable, индексы и лимит фрагмента — по спеке B7 (D1)."""
    assert OfftopicRefusal.__tablename__ == 'offtopic_refusals'
    assert OfftopicRefusal.FRAGMENT_LIMIT == 120

    cols = OfftopicRefusal.__table__.columns
    assert cols['id'].primary_key is True
    assert cols['user_id'].nullable is False
    assert cols['user_id'].index is True
    assert cols['user_id'].type.length == 255
    assert cols['reason'].nullable is False
    assert cols['reason'].type.length == 32
    assert cols['message_fragment'].nullable is False
    assert cols['message_fragment'].type.length == 120
    # created_at: timezone-aware колонка с default, server_default и
    # индексом для фильтра периода (образец — RtScore.fetched_at)
    assert cols['created_at'].nullable is False
    assert cols['created_at'].type.timezone is True
    assert cols['created_at'].default is not None
    assert cols['created_at'].server_default is not None
    assert cols['created_at'].index is True


@pytest.fixture()
def sqlite_app():
    """Реальная in-memory SQLite: create_all/drop_all вокруг теста."""
    app = Flask('test_offtopic_sqlite')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_create_all_creates_offtopic_refusals(sqlite_app):
    """create_all создаёт таблицу offtopic_refusals с ожидаемыми колонками."""
    with sqlite_app.app_context():
        inspector = inspect(db.engine)
        assert 'offtopic_refusals' in inspector.get_table_names()
        column_names = {c['name'] for c in inspector.get_columns('offtopic_refusals')}
        assert {'id', 'user_id', 'reason', 'message_fragment', 'created_at'} <= column_names


def test_init_db_has_no_raw_sql_and_mentions_offtopic_refusals():
    """init_db.py: схема НЕ дублируется сырым SQL, таблица создаётся create_all."""
    text = (ROOT / 'init_db.py').read_text(encoding='utf-8')
    assert 'CREATE TABLE' not in text.upper()
    assert 'db.create_all()' in text
    assert 'offtopic_refusals' in text.lower()


# === 2. record_refusal: запись, обрезка фрагмента, fail-silent ===


def test_record_refusal_writes_row_and_truncates_fragment(sqlite_app):
    """Запись строки через app=sqlite_app; фрагмент >120 обрезается (приватность)."""
    long_message = 'ы' * 300
    refusal_tracker.record_refusal('u1', 'offtopic', long_message, app=sqlite_app)
    with sqlite_app.app_context():
        rows = db.session.query(OfftopicRefusal).all()
        assert len(rows) == 1
        assert rows[0].user_id == 'u1'
        assert rows[0].reason == 'offtopic'
        assert rows[0].message_fragment == 'ы' * 120
        assert len(rows[0].message_fragment) <= OfftopicRefusal.FRAGMENT_LIMIT
        assert rows[0].created_at is not None


def test_record_refusal_fail_silent_and_warns_once(monkeypatch, caplog):
    """Сбой БД: исключение НЕ пробрасывается, warning один раз (флаг _db_warned)."""
    class BoomApp:
        def app_context(self):
            raise RuntimeError('БД недоступна')

    monkeypatch.setattr(refusal_tracker, '_db_warned', False)
    caplog.set_level(logging.DEBUG)
    # Не кидает исключение — диалог пользователя не прерывается
    refusal_tracker.record_refusal('u1', 'llm_offtopic', 'сообщение', app=BoomApp())
    refusal_tracker.record_refusal('u2', 'llm_offtopic', 'сообщение', app=BoomApp())
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and 'метрику отказа' in r.getMessage()]
    assert len(warnings) == 1


# === 3. Агрегация топ-N фрагментов за период ===


def test_get_top_fragments_groups_filters_and_orders(sqlite_app):
    """Группировка fragment+reason, count, last_seen, фильтр cutoff, limit."""
    now = datetime.now(timezone.utc)
    with sqlite_app.app_context():
        db.session.add_all([
            # Дубль фрагмента от двух пользователей — счётчик 2 (спека:
            # «одинаковые посторонние запросы» дают одну строку топа)
            OfftopicRefusal(user_id='u1', reason='offtopic',
                            message_fragment='какая погода', created_at=now - timedelta(hours=1)),
            OfftopicRefusal(user_id='u2', reason='offtopic',
                            message_fragment='какая погода', created_at=now),
            OfftopicRefusal(user_id='u3', reason='llm_offtopic',
                            message_fragment='напиши код', created_at=now - timedelta(hours=2)),
            # Запись старше периода (10 дней) — отсекается cutoff=7 дней
            OfftopicRefusal(user_id='u4', reason='offtopic',
                            message_fragment='старое сообщение', created_at=now - timedelta(days=10)),
        ])
        db.session.commit()

        cutoff = now - timedelta(days=7)
        top = OfftopicRefusal.get_top_fragments(cutoff)
        assert [(t[0], t[1], t[2]) for t in top] == [
            ('какая погода', 'offtopic', 2),
            ('напиши код', 'llm_offtopic', 1),
        ]
        # last_seen — момент последней фиксации (max created_at)
        assert top[0][3] is not None

        # limit уважается, сортировка по count desc сохраняется
        top1 = OfftopicRefusal.get_top_fragments(cutoff, limit=1)
        assert len(top1) == 1
        assert top1[0][2] == 2


def test_get_top_fragments_tie_break_by_last_seen(sqlite_app):
    """Равные count: детерминированный порядок — свежая фиксация выше."""
    now = datetime.now(timezone.utc)
    with sqlite_app.app_context():
        db.session.add_all([
            OfftopicRefusal(user_id='u1', reason='offtopic',
                            message_fragment='старый дубль', created_at=now - timedelta(hours=5)),
            OfftopicRefusal(user_id='u2', reason='offtopic',
                            message_fragment='свежий дубль', created_at=now - timedelta(minutes=5)),
        ])
        db.session.commit()
        top = OfftopicRefusal.get_top_fragments(now - timedelta(days=1))
        # Счётчики равны (по 1) — вторичная сортировка max(created_at) desc
        # убирает «мерцание» топ-N между одинаковыми запросами
        assert [(t[0], t[2]) for t in top] == [
            ('свежий дубль', 1),
            ('старый дубль', 1),
        ]


# === 4. Интеграция с process_message: обе ветки отказа ===


def _install_refusal_spy(monkeypatch):
    """Spy вместо record_refusal в пространстве имён dialogue_manager."""
    calls = []

    def spy(user_id, reason, message, app=None):
        calls.append((user_id, reason, message, app))

    monkeypatch.setattr(dm_module, 'record_refusal', spy)
    return calls


def test_process_message_precheck_block_records_refusal(monkeypatch):
    """Ветка precheck: «напиши код на python» → отказ + запись с причиной offtopic."""
    dm = make_manager()
    calls = _install_refusal_spy(monkeypatch)
    result = run_coro(dm.process_message(None, '42', 'напиши код на python'))
    assert result['response'] == REFUSAL_OFFTOPIC
    assert len(calls) == 1
    user_id, reason, message, app = calls[0]
    assert user_id == '42'
    assert reason == 'offtopic'
    assert 'напиши код' in message
    # StubSessionManager не имеет _app → None, трекер взял бы своё ленивое
    assert app is None


def test_process_message_llm_offtopic_records_refusal(monkeypatch):
    """Ветка llm_offtopic: классификатор вернул offtopic → запись с причиной llm_offtopic."""
    dm = make_manager()
    calls = _install_refusal_spy(monkeypatch)

    async def fake_classify(http_session, message, context):
        return {'intent': 'offtopic'}

    monkeypatch.setattr(dm.intent_classifier, 'classify_with_llm', fake_classify)
    result = run_coro(dm.process_message(None, '42', 'расскажи про свою жизнь'))
    assert result['response'] == REFUSAL_OFFTOPIC
    assert len(calls) == 1
    user_id, reason, message, app = calls[0]
    assert user_id == '42'
    assert reason == 'llm_offtopic'
    assert 'расскажи про свою жизнь' in message


# === 5. Админ-роут /admin/offtopic ===


@pytest.fixture()
def admin_app():
    """Минимальное Flask-приложение: admin_bp + SQLite, админ и отказ в БД."""
    app = Flask('test_offtopic_admin', template_folder=str(TEMPLATES_DIR))
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = 'test-secret-key'
    db.init_app(app)

    from admin_routes import admin_bp
    app.register_blueprint(admin_bp, url_prefix='/admin')

    login_manager = LoginManager(app)
    login_manager.login_view = 'admin.admin_login'

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    with app.app_context():
        db.create_all()
        role = Role(name='admin', description='Администратор системы')
        db.session.add(role)
        db.session.commit()
        user = User(username='admin', email='admin@test.local', role_id=role.id)
        user.set_password('test-password-123')
        db.session.add(user)
        db.session.add(OfftopicRefusal(
            user_id='u1', reason='offtopic',
            message_fragment='фрагмент-маркер-погоды',
        ))
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_admin_offtopic_route_requires_login(admin_app):
    """Анонимный доступ: редирект на вход, данные отказов не раскрываются."""
    client = admin_app.test_client()
    response = client.get('/admin/offtopic')
    assert response.status_code == 302
    assert '/admin/login' in response.headers['Location']


def test_admin_offtopic_route_renders_fragments(admin_app):
    """Админ: логин → GET /admin/offtopic?days=7 → 200 и фрагмент в теле."""
    client = admin_app.test_client()
    login = client.post('/admin/login', data={
        'username': 'admin',
        'password': 'test-password-123',
    }, follow_redirects=False)
    assert login.status_code == 302  # успешный вход → редирект на dashboard

    response = client.get('/admin/offtopic?days=7')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'фрагмент-маркер-погоды' in body
    assert 'Офтопик (precheck)' in body  # человекочитаемая подпись причины
    assert '120' in body  # примечание о приватности
    # Валидный HTML (tasks.md 3.2): ключевые закрывающие теги на месте.
    # Полная балансировка через assert_html_balanced неприменима к целой
    # странице: чекер не знает void-элементы (<meta>, <link>) шаблона.
    assert '</table>' in body
    assert '</body>' in body
    assert '</html>' in body


def test_admin_offtopic_route_escapes_html_in_fragment(admin_app):
    """Регрессия XSS (приватность/экранирование — критерий приёмки B7).

    Пользовательский фрагмент с HTML/JS должен рендериться ЭКРАНИРОВАННО:
    Jinja2 autoescape в шаблоне admin/offtopic.html ({{ fragment }} без
    |safe) neutralизует ввод — сырой <script> не исполняется браузером.
    """
    client = admin_app.test_client()
    login = client.post('/admin/login', data={
        'username': 'admin',
        'password': 'test-password-123',
    }, follow_redirects=False)
    assert login.status_code == 302  # успешный вход → редирект на dashboard

    # Запись с враждебным фрагментом — через ORM, как в фикстуре admin_app
    with admin_app.app_context():
        db.session.add(OfftopicRefusal(
            user_id='u-xss', reason='llm_offtopic',
            message_fragment='<script>alert(1)</script> <b>жирный</b>',
        ))
        db.session.commit()

    response = client.get('/admin/offtopic?days=7')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    # Сырых тегов пользователя в теле НЕТ — autoescape сработал
    assert '<script>' not in body
    assert '<b>жирный</b>' not in body
    # Экранированная форма присутствует (подтверждение escape, а не удаления)
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in body
    assert '&lt;b&gt;жирный&lt;/b&gt;' in body


def test_admin_offtopic_route_empty_period(admin_app):
    """Пустое хранилище за период: страница без ошибок, признак отсутствия данных."""
    client = admin_app.test_client()
    client.post('/admin/login', data={
        'username': 'admin',
        'password': 'test-password-123',
    })
    # Пустое хранилище: удаляем записи и проверяем alert-ветку шаблона
    # («За выбранный период отказов не зафиксировано») без ошибок рендера.
    with admin_app.app_context():
        db.session.query(OfftopicRefusal).delete()
        db.session.commit()
    response = client.get('/admin/offtopic?days=30')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'отказов не зафиксировано' in body
    assert 'фрагмент-маркер-погоды' not in body
