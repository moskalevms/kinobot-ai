# src/llm/llm_router.py
import os
from typing import Optional, List, Dict
from .gigachat_client import GigaChatClient

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
                print("[LLM] ✅ GigaChat добавлен (используется GIGACHAT_AUTH_KEY для получения токена)")
            except Exception as e:
                print(f"[LLM] ❌ Ошибка при инициализации GigaChat: {e}")
        else:
            print("[LLM] ⚠️ GIGACHAT_AUTH_KEY не указан — GigaChat отключен")

        # 🔽 Временно отключаем DeepSeek через флаг (на будущее)
        enable_deepseek = os.getenv("ENABLE_DEEPSEEK", "false").lower() == "true"
        if enable_deepseek:
            deepseek_key = os.getenv("DEEPSEEK_API_KEY")
            deepseek_base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").strip()  # ← исправлено
            if deepseek_key:
                try:
                    from openai import OpenAI
                    self.models.append({
                        "name": "deepseek",
                        "client": OpenAI(api_key=deepseek_key, base_url=deepseek_base),
                        "type": "openai"
                    })
                    print("[LLM] ✅ DeepSeek добавлен")
                except ImportError:
                    print("[LLM] ❌ Модуль openai не установлен — DeepSeek недоступен")
            else:
                print("[LLM] ⚠️ DEEPSEEK_API_KEY не указан — DeepSeek не будет использоваться даже при ENABLE_DEEPSEEK=true")

        if not self.models:
            raise ValueError("Не указаны ключи API для GigaChat (GIGACHAT_API_KEY) или DeepSeek (если включён)")

    def call_llm(self, messages: List[Dict[str, str]], max_tokens: int = 500) -> Optional[str]:
        for model in self.models:
            try:
                print(f"[LLM] Пробуем {model['name']}...")
                if model["type"] == "gigachat":
                    result = model["client"].chat_completions_create(
                        model="GigaChat",
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=0.3
                    )
                else:  # openai-совместимый (DeepSeek, если включён)
                    response = model["client"].chat.completions.create(
                        model="deepseek-chat",
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=0.3,
                        timeout=30
                    )
                    result = response.choices[0].message.content.strip()

                print(f"[LLM] ✅ Успешный ответ от {model['name']}")
                return result
            except Exception as e:
                print(f"[LLM] ❌ {model['name']} недоступен: {e}")
                continue

        print("[LLM] ❌ Все LLM недоступны")
        return None