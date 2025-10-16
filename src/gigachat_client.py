# src/gigachat_client.py
import os
import ssl
import aiohttp
import logging
from time import time
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

# Отключаем предупреждения SSL (Sber использует самоподписанные сертификаты)
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE


class GigaChatClient:
    def __init__(self):
        self.auth_key = os.getenv("GIGACHAT_AUTH_KEY")
        if not self.auth_key:
            raise ValueError("GIGACHAT_AUTH_KEY не указан в .env")
        self.auth_url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
        self.api_url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
        self.access_token = None
        self.token_expires_at = 0

    async def _get_token(self, session: aiohttp.ClientSession) -> str:
        if self.access_token and time() < self.token_expires_at:
            return self.access_token

        headers = {
            'RqUID': str(__import__('uuid').uuid4()),
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json',
            'Authorization': f'Basic {self.auth_key}'
        }
        data = {'scope': 'GIGACHAT_API_PERS'}

        try:
            async with session.post(
                self.auth_url,
                headers=headers,
                data=data,
                ssl=ssl_context
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"Ошибка получения токена: HTTP {resp.status}, {error_text}")
                token_data = await resp.json()
                self.access_token = token_data['access_token']
                expires_in = token_data.get('expires_in', 1800)
                self.token_expires_at = time() + expires_in - 60
                logger.info(f"[GigaChat] ✅ Получен новый access_token (действует {expires_in // 60} мин)")
                return self.access_token
        except Exception as e:
            logger.error(f"[GigaChat] Ошибка получения токена: {e}")
            raise

    async def chat_completions_create(
        self,
        session: aiohttp.ClientSession,
        model: str = "GigaChat",
        messages: list = None,
        max_tokens: int = 500,
        temperature: float = 0.7
    ) -> str:
        token = await self._get_token(session)
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        payload = {
            "model": model,
            "messages": messages or [],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False
        }
        try:
            async with session.post(
                self.api_url,
                headers=headers,
                json=payload,
                ssl=ssl_context
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"Ошибка GigaChat API: HTTP {resp.status}, {error_text}")
                result = await resp.json()
                return result['choices'][0]['message']['content'].strip()
        except Exception as e:
            logger.error(f"[GigaChat] Ошибка вызова API: {e}")
            raise