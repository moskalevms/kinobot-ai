# Dockerfile

# Шаг 1: Используем официальный легковесный Python-образ
FROM python:3.11-slim

# Шаг 2: Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Шаг 3: Копируем requirements.txt и устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Шаг 4: Копируем весь исходный код
COPY src/ ./src/

# Шаг 5: Запускаем бота в режиме polling (плоские импорты, запуск без -m)
CMD ["python", "src/telegram_bot.py"]