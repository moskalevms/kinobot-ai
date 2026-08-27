import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# Тестовые значения ключей: без них конструкторы клиентов не создать,
# сетевых вызовов в тестах нет
os.environ.setdefault('GIGACHAT_AUTH_KEY', 'test-gigachat-key')
os.environ.setdefault('KINOPOISK_API_KEY', 'test-kinopoisk-key')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-telegram-token')
