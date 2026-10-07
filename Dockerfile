# Dockerfile

# Шаг 1: Используем официальный легковесный Python-образ
FROM python:3.11-slim

# Шаг 2: Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# UTF-8 независимо от локали базового образа: alembic читает alembic.ini в
# кодировке локали, а файл хранится в UTF-8 (русские комментарии) — переменная
# гарантирует читаемость ini для CLI `alembic upgrade head` (шаг деплоя) и
# снимает риск при смене базового образа (стартап-хелпер бота читает ini
# явно в UTF-8 — src/db_migrations.py).
ENV PYTHONUTF8=1

# Шаг 3: Копируем requirements.txt и устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Шаг 4: Копируем исходный код, скрипт инициализации БД и файлы миграций Alembic
# (бэклог T11/T12): compose запускает образ, а не исходники, поэтому
# `alembic upgrade head` внутри контейнера (шаг деплоя) и стартап-хелпер бота
# `src/db_migrations.py` обязаны находить alembic.ini и migrations/ в /app.
COPY src/ ./src/
COPY init_db.py .
COPY migrations/ ./migrations/
COPY alembic.ini .

# Шаг 5: Запускаем бота в режиме polling (плоские импорты, запуск без -m)
CMD ["python", "src/telegram_bot.py"]