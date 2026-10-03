# scripts/cleanup_stats.py
# Одноразовая очистка таблицы клиентской статистики user_statistics.
# Применяется при внедрении изменения client-only-statistics: удаляет
# накопленные записи, созданные запросами администратора к админ-панели.
import os

from dotenv import load_dotenv

# .env лежит в корне проекта (на уровень выше scripts/)
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from sqlalchemy import create_engine, text

DEFAULT_DATABASE_URL = 'postgresql://postgres:postgres@localhost:5432/kinobot_db'


def main():
    database_url = os.getenv('DATABASE_URL', DEFAULT_DATABASE_URL)
    engine = create_engine(database_url)
    with engine.begin() as conn:
        count = conn.execute(
            text('SELECT COUNT(*) FROM user_statistics')
        ).scalar()
        print(f"Записей в user_statistics до очистки: {count}")
        result = conn.execute(text('DELETE FROM user_statistics'))
        print(f"Удалено строк: {result.rowcount}")
    print("✅ Таблица user_statistics очищена")


if __name__ == '__main__':
    main()
