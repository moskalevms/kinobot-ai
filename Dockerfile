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

# Шаг 5: Запускаем бота как модуль (в режиме polling)
CMD ["python", "-m", "src.telegram_bot"]