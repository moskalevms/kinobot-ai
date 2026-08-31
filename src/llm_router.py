# src/llm_router.py
import os
import logging
from typing import Optional, List, Dict
from gigachat_client import GigaChatClient

logger = logging.getLogger(__name__)


class LLMRouter:
    def __init__(self):
        self.models = []
        gigachat_auth_key = os.getenv("GIGACHAT_AUTH_KEY")
        if gigachat_auth_key:
            try:
                self.models.append({
                    "name": "gigachat",
                    "client": GigaChatClient(),
                    "type": "gigachat"
                })
                logger.info("[LLM] ✅ GigaChat добавлен")
            except Exception as e:
                logger.error(f"[LLM] ❌ Ошибка при инициализации GigaChat: {e}")
        else:
            logger.warning("[LLM] ⚠️ GIGACHAT_AUTH_KEY не указан — GigaChat отключен")

        enable_deepseek = os.getenv("ENABLE_DEEPSEEK", "false").lower() == "true"
        if enable_deepseek:
            deepseek_key = os.getenv("DEEPSEEK_API_KEY")
            deepseek_base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").strip()
            if deepseek_key:
                try:
                    from openai import AsyncOpenAI
                    self.models.append({
                        "name": "deepseek",
                        "client": AsyncOpenAI(api_key=deepseek_key, base_url=deepseek_base),
                        "type": "openai"
                    })
                    logger.info("[LLM] ✅ DeepSeek добавлен")
                except ImportError:
                    logger.error("[LLM] ❌ Модуль openai не установлен — DeepSeek недоступен")
            else:
                logger.warning("[LLM] ⚠️ DEEPSEEK_API_KEY не указан")

        if not self.models:
            raise ValueError("Не указаны ключи API для LLM")

    async def call_llm(self, session, messages: List[Dict[str, str]], max_tokens: int = 500) -> Optional[str]:
        for model in self.models:
            try:
                logger.info(f"[LLM] Пробуем {model['name']}...")
                if model["type"] == "gigachat":
                    result = await model["client"].chat_completions_create(
                        session=session,
                        model="GigaChat",
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=0.3
                    )
                else:  # openai-совместимый
                    response = await model["client"].chat.completions.create(
                        model="deepseek-chat",
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=0.3,
                        timeout=30
                    )
                    result = response.choices[0].message.content.strip()
                logger.info(f"[LLM] ✅ Успешный ответ от {model['name']}")
                return result
            except Exception as e:
                logger.warning(f"[LLM] ❌ {model['name']} недоступен: {e}")
                continue
        logger.error("[LLM] ❌ Все LLM недоступны")
        return None
