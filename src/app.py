# src/app.py
import os
import sys
import logging

# Фиксируем директорию
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, render_template, request, jsonify
from llm.dialog_agent import DialogMovieAgent

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__,
            template_folder='templates',
            static_folder='static')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get('message', '').strip()

    if not user_message:
        return jsonify({"error": "Сообщение не может быть пустым"}), 400

    try:
        dialog_agent = DialogMovieAgent()  # для MVP — ок
        result = dialog_agent.chat(user_message)

        return jsonify({
            "response": result["response"],
            "needs_clarification": result.get("needs_clarification", False),
            "parameters": result.get("parameters", {})
        })

    except Exception as e:
        logger.error(f"[APP] Ошибка при обработке запроса: {e}")
        return jsonify({"error": f"Произошла ошибка: {str(e)}"}), 500

@app.route('/health')
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    logger.info("Запуск приложения в режиме разработки...")
    app.run(debug=True, host='0.0.0.0', port=5000)