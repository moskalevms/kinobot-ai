# src/app.py
import os
import sys
import logging
import uuid
import asyncio
from datetime import timedelta
from flask import Flask, render_template, request, jsonify, session
from flask_executor import Executor
from flask_login import LoginManager
import aiohttp

# Настройка путей
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
from session_manager import SessionManager
from dialogue_manager import DialogueManager
from models.database import db, User, UserStatistics

load_dotenv()
from log_setup import setup_logging

logger = setup_logging("web")

app = Flask(__name__, template_folder='templates', static_folder='static')

# Секретный ключ сессий: обязателен в продакшене, временный в разработке
FLASK_ENV = os.getenv('FLASK_ENV', 'development')
_secret_key = os.getenv('FLASK_SECRET_KEY')
if not _secret_key:
    if FLASK_ENV == 'production':
        raise RuntimeError(
            "FLASK_SECRET_KEY не задан: запуск в продакшене без секретного ключа запрещён"
        )
    _secret_key = 'kinobot_dev_temporary_key'
    logger.warning(
        "FLASK_SECRET_KEY не задан — используется временный ключ, только для разработки"
    )
app.secret_key = _secret_key

# Настройка БД
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
    'DATABASE_URL',
    'postgresql://postgres:postgres@localhost:5432/kinobot_db'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=31)

# Инициализация БД
db.init_app(app)

# Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'admin_login'


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# Инициализация менеджеров (сессии — в PostgreSQL текущего приложения)
session_manager = SessionManager(app)
dialogue_manager = DialogueManager(session_manager)

# Executor для запуска async-функций в Flask
executor = Executor(app)


@app.before_request
def make_session_permanent():
    session.permanent = True


@app.before_request
def ensure_user_id():
    if 'user_id' not in session:
        session['user_id'] = str(uuid.uuid4())
        logger.info(f"Создан новый user_id: {session['user_id']}")


@app.before_request
def track_statistics():
    """Отслеживание статистики пользователей"""
    if request.endpoint and not request.endpoint.startswith('admin_') and not request.endpoint.startswith('static'):
        user_id = session.get('user_id')
        if user_id:
            user_agent = request.headers.get('User-Agent')
            ip_address = request.remote_addr
            UserStatistics.track_user(user_id, user_agent, ip_address)


@app.route('/')
def index():
    return render_template('index.html')


async def _process_chat_async(user_id: str, user_message: str) -> dict:
    async with aiohttp.ClientSession() as http_session:
        result = await dialogue_manager.process_message(http_session, user_id, user_message)
        return result


@app.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400
        user_message = data.get('message', '').strip()
        if not user_message:
            return jsonify({"error": "Сообщение не может быть пустым"}), 400
        user_id = session['user_id']
        logger.info(f"Обработка сообщения от {user_id}: {user_message}")
        future = executor.submit(asyncio.run, _process_chat_async(user_id, user_message))
        result = future.result()
        response_data = {
            "response": result.get("response", "Произошла ошибка"),
            "needs_clarification": result.get("needs_clarification", False)
        }
        if "movie" in result:
            response_data["movie"] = result["movie"]
        if "parameters" in result:
            response_data["parameters"] = result["parameters"]
        logger.info(f"Ответ для {user_id}: needs_clarification={response_data['needs_clarification']}")
        return jsonify(response_data)
    except Exception as e:
        logger.error(f"Ошибка в /chat: {e}", exc_info=True)
        return jsonify({
            "response": "Извините, произошла внутренняя ошибка. Попробуйте еще раз.",
            "needs_clarification": True
        }), 500


@app.route('/new-chat', methods=['POST'])
def new_chat():
    try:
        user_id = session['user_id']
        dialogue_manager.clear_user_session(user_id)
        logger.info(f"Новый чат для пользователя {user_id}")
        return jsonify({
            "status": "success",
            "message": "История диалога очищена"
        })
    except Exception as e:
        logger.error(f"Ошибка в /new-chat: {e}")
        return jsonify({"status": "error"}), 500


async def _health_check_async() -> bool:
    async with aiohttp.ClientSession() as http_session:
        return await dialogue_manager.movie_agent.health_check(http_session)


@app.route('/health')
def health():
    try:
        future = executor.submit(asyncio.run, _health_check_async())
        healthy = future.result()
        if healthy:
            return jsonify({
                "status": "healthy",
                "sessions_count": session_manager.count_sessions()
            }), 200
        else:
            return jsonify({"status": "unhealthy"}), 503
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({"status": "error"}), 500


# Импорт админ-роутов
from admin_routes import admin_bp

app.register_blueprint(admin_bp, url_prefix='/admin')


if __name__ == '__main__':
    logger.info("Запуск Flask приложения...")
    if FLASK_ENV != 'production':
        logging.basicConfig(level=logging.DEBUG)
    app.run(debug=(FLASK_ENV != 'production'), host='0.0.0.0', port=5000)
