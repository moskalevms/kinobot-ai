# src/llm/gigachat_client.py
import os
import requests
import uuid
from time import time

class GigaChatClient:
    def __init__(self):
        self.auth_key = os.getenv("GIGACHAT_AUTH_KEY")
        if not self.auth_key:
            raise ValueError("GIGACHAT_AUTH_KEY не указан в .env")

        self.auth_url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
        self.api_url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

        self.access_token = None
        self.token_expires_at = 0

    def _get_token(self):
        # Возвращаем токен, если он ещё действителен
        if self.access_token and time() < self.token_expires_at:
            return self.access_token

        RqUID = str(uuid.uuid4())

        headers = {
            'RqUID': RqUID,
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json',
            'Authorization': f'Basic {self.auth_key}'
        }

        data = {
            'scope': 'GIGACHAT_API_PERS'
        }

        try:
            # ⚠️ verify=False — временно, для обхода SSL-ошибок (Sber использует самоподписанные сертификаты)
            response = requests.post(
                self.auth_url,
                headers=headers,
                data=data,
                verify=False
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            error_detail = response.text if 'response' in locals() else 'N/A'
            raise Exception(f"Ошибка получения токена GigaChat: {e}\nОтвет сервера: {error_detail}")

        token_data = response.json()
        self.access_token = token_data['access_token']
        expires_in = token_data.get('expires_in', 1800)  # по умолчанию 30 минут
        self.token_expires_at = time() + expires_in - 60  # обновляем за минуту до истечения

        print(f"[GigaChat] ✅ Получен новый access_token (действует {expires_in//60} мин)")

        return self.access_token

    def chat_completions_create(self, model: str, messages: list, max_tokens: int = 500, temperature: float = 0.7):
        token = self._get_token()

        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False
        }

        try:
            response = requests.post(
                self.api_url,
                headers=headers,
                json=payload,
                verify=False
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            error_detail = response.text if 'response' in locals() else 'N/A'
            raise Exception(f"Ошибка вызова GigaChat API: {e}\nОтвет сервера: {error_detail}")

        result = response.json()
        return result['choices'][0]['message']['content'].strip()