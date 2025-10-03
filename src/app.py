# src/app.py
import os
import sys
import logging
import uuid
from flask import Flask, render_template, request, jsonify, session

# Настройка путей
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
from session_manager import SessionManager
from dialogue_manager import DialogueManager

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or 'kinobot_dev_secret_key_2025'

# Инициализация менеджеров
session_manager = SessionManager()
dialogue_manager = DialogueManager(session_manager)


@app.before_request
def make_session_permanent():
    """Сделать сессию постоянной"""
    session.permanent = True


@app.before_request
def ensure_user_id():
    """Убедиться, что у пользователя есть ID"""
    if 'user_id' not in session:
        session['user_id'] = str(uuid.uuid4())
        logger.info(f"Создан новый user_id: {session['user_id']}")


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/chat', methods=['POST'])
def chat():
    """Основной endpoint для чата"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400

        user_message = data.get('message', '').strip()
        if not user_message:
            return jsonify({"error": "Сообщение не может быть пустым"}), 400

        user_id = session['user_id']
        logger.info(f"Обработка сообщения от {user_id}: {user_message}")

        # Обрабатываем сообщение
        result = dialogue_manager.process_message(user_id, user_message)

        # Подготавливаем ответ
        response_data = {
            "response": result.get("response", "Произошла ошибка"),
            "needs_clarification": result.get("needs_clarification", False)
        }

        # Добавляем дополнительные данные если есть
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
    """Начать новый диалог (очистить историю)"""
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


@app.route('/health')
def health():
    """Health check endpoint"""
    try:
        # Проверяем основные компоненты
        healthy = dialogue_manager.movie_agent.health_check()

        if healthy:
            return jsonify({
                "status": "healthy",
                "sessions_count": len(session_manager.sessions)
            }), 200
        else:
            return jsonify({"status": "unhealthy"}), 503

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({"status": "error"}), 500


@app.route('/debug/session')
def debug_session():
    """Debug endpoint для просмотра сессии (только для разработки)"""
    if not app.debug:
        return jsonify({"error": "Not available in production"}), 403

    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"error": "No session"})

    user_session = session_manager.get_session(user_id)
    return jsonify(user_session.to_dict())


if __name__ == '__main__':
    logger.info("Запуск Flask приложения...")
    logging.basicConfig(level=logging.DEBUG)
    app.run(debug=True, host='0.0.0.0', port=5000)