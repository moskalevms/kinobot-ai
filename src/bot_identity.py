# src/bot_identity.py
import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger(__name__)

# Кэш результата (в т.ч. ошибок) — чтобы не запрашивать API
# при каждом открытии дашборда
_CACHE_TTL_SECONDS = 15 * 60
_cache = {"value": None, "expires_at": 0.0}


def _fetch_bot_info() -> dict | None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN не задан — данные бота недоступны")
        return None
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning(f"Не удалось получить данные бота: {e}")
        return None
    if not data.get("ok"):
        logger.warning(f"Telegram API вернул ошибку: {data}")
        return None
    result = data.get("result", {})
    return {
        "username": result.get("username"),
        "first_name": result.get("first_name"),
    }


def get_current_bot() -> dict | None:
    """Текущий подключённый бот: {'username', 'first_name'} либо None."""
    now = time.monotonic()
    if now >= _cache["expires_at"]:
        _cache["value"] = _fetch_bot_info()
        _cache["expires_at"] = now + _CACHE_TTL_SECONDS
    return _cache["value"]
