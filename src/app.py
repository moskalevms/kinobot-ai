# src/app.py
import os
import sys
import logging
import uuid
import asyncio
from flask import Flask, render_template, request, jsonify, session
from flask_executor import Executor
import aiohttp
from telegram import Update
from telegram.ext import Application

# Настройка путей
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
from session_manager import SessionManager
from dialogue_manager import DialogueManager
from telegram_bot import create_telegram_app, setup_webhook

load_dotenv()
from log_setup import setup_logging
logger = setup_logging("web")

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or 'kinobot_dev_secret_key_2025'

# Инициализация менеджеров
session_manager = SessionManager()
dialogue_manager = DialogueManager(session_manager)

# Executor для запуска async-функций в Flask
executor = Executor(app)

# Глобальное приложение Telegram (для webhook)
telegram_app: Application = None

BOT_MODE = os.getenv("BOT_MODE", "polling")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

if BOT_MODE == "webhook":
    if not WEBHOOK_URL:
        raise ValueError("WEBHOOK_URL обязателен в режиме webhook")
    telegram_app = create_telegram_app()
    setup_webhook(telegram_app, WEBHOOK_URL)
elif BOT_MODE != "polling":
    raise ValueError(f"Неизвестный BOT_MODE: {BOT_MODE}")

@app.before_request
def make_session_permanent():
    session.permanent = True

@app.before_request
def ensure_user_id():
    if 'user_id' not in session:
        session['user_id'] = str(uuid.uuid4())
        logger.info(f"Создан новый user_id: {session['user_id']}")

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
                "sessions_count": len(session_manager.sessions)
            }), 200
        else:
            return jsonify({"status": "unhealthy"}), 503
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({"status": "error"}), 500

@app.route('/debug/session')
def debug_session():
    if not app.debug:
        return jsonify({"error": "Not available in production"}), 403
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"error": "No session"})
    user_session = session_manager.get_session(user_id)
    return jsonify(user_session.to_dict())

# Webhook endpoint для Telegram
@app.route('/telegram-webhook', methods=['POST'])
def telegram_webhook():
    global telegram_app
    if telegram_app is None:
        return jsonify({"error": "Telegram app not initialized"}), 500
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    telegram_app.update_queue.put_nowait(update)
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    logger.info("Запуск Flask приложения...")
    logging.basicConfig(level=logging.DEBUG)
    app.run(debug=True, host='0.0.0.0', port=5000)