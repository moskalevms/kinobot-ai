# src/app.py
import os
import sys
import logging

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, render_template, request, jsonify, session
from llm.dialog_agent import DialogMovieAgent
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or 'kinobot_dev_secret_key_2025'

@app.route('/')
def index():
    return render_template('index.html')

# ... (импорты без изменений) ...

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get('message', '').strip()
    if not user_message:
        return jsonify({"error": "Сообщение не может быть пустым"}), 400

    try:
        dialog_agent = DialogMovieAgent()
        result = dialog_agent.chat(user_message, data.get('history', []))

        if not result.get("needs_clarification"):
            if result.get("movies_list"):
                simplified = []
                for m in result["movies_list"]:
                    simplified.append({
                        'id': m.get('id'),
                        'title': m.get('title'),
                        'year': m.get('year'),
                        'genre': m.get('genre'),
                        'country': m.get('country'),
                        'rating_imdb': m.get('rating_imdb'),
                        'rating_kp': m.get('rating_kp')
                    })
                session['last_movies'] = simplified
            session['last_params'] = result.get("parameters", {})
            actor = result["parameters"].get("actor")
            if actor:
                session['last_actor'] = actor

        return jsonify({
            "response": result["response"],
            "needs_clarification": result.get("needs_clarification", False),
            "parameters": result.get("parameters", {}),
            "movie": result.get("movie", None)
        })

    except Exception as e:
        logger.error(f"[APP] Ошибка: {e}", exc_info=True)
        return jsonify({"error": "Произошла ошибка"}), 500

@app.route('/movie-details', methods=['POST'])
def movie_details():
    data = request.json
    movie_id = data.get('movie_id')
    title = data.get('title', 'Фильм')

    try:
        dialog_agent = DialogMovieAgent()
        movie = None
        # Поиск по ID (если числовой)
        if movie_id and str(movie_id).isdigit():
            movie = dialog_agent.movie_agent.get_movie_by_id(movie_id)
        # Fallback: поиск по названию
        if not movie:
            found = dialog_agent.movie_agent.search_by_title(title)
            movie = found[0] if found else None

        if movie:
            prompt_template = dialog_agent._load_prompt('response_generation_prompt.txt')
            prompt = prompt_template.format(
                title=movie.get('title', '—'),
                year=movie.get('year', '—'),
                genre=movie.get('genre', '—'),
                rating=movie.get('rating', '—'),
                description=movie.get('description', 'Описание отсутствует.')
            )
            messages = [{"role": "user", "content": prompt}]
            response_text = dialog_agent.llm_router.call_llm(messages, max_tokens=300)
            response_text = response_text.strip() if response_text else f"🎬 <strong>{movie['title']}</strong> ({movie['year']}) — ⭐ {movie['rating']}"
        else:
            response_text = f"Не нашёл подробностей о «{title}»."

        return jsonify({"response": response_text})

    except Exception as e:
        logger.error(f"[MOVIE-DETAILS] Ошибка: {e}")
        return jsonify({"response": f"Ошибка при загрузке «{title}»."}), 500

@app.route('/new-chat', methods=['POST'])
def new_chat():
    session.clear()
    return jsonify({"status": "ok"})

@app.route('/health')
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    logger.info("Запуск...")
    app.run(debug=True, host='0.0.0.0', port=5000)