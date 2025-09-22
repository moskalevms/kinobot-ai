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

# Шаг 5: Экспонируем порт (Flask по умолчанию слушает 5000)
EXPOSE 5000

# Шаг 6: Запускаем приложение
# Используем gunicorn для продакшн-сервера (лучше, чем встроенный Flask-сервер)
# Если gunicorn не указан в requirements.txt — добавим его ниже
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "src.app:app"]