# src/llm/dialog_agent.py
import os
from .llm_router import LLMRouter

class DialogMovieAgent:
    def __init__(self):
        self.llm_router = LLMRouter()
        self.prompts_dir = os.path.join(os.path.dirname(__file__), '..', 'prompts')

    def _load_prompt(self, filename: str) -> str:
        path = os.path.join(self.prompts_dir, filename)
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()

    def chat(self, user_message: str) -> dict:
        system_prompt = self._load_prompt('system_movie_agent.txt')
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ]

        response = self.llm_router.call_llm(messages, max_tokens=300)

        if not response:
            return {
                "response": "К сожалению, сейчас я не могу ответить. Попробуй позже.",
                "needs_clarification": False,
                "parameters": {}
            }

        # Здесь можно добавить логику анализа, нужно ли уточнение
        # Например, если ответ содержит вопрос — ставим needs_clarification=True

        return {
            "response": response,
            "needs_clarification": "?" in response,  # упрощённая логика
            "parameters": {}  # пока пусто, в будущем — извлечённые параметры фильма
        }