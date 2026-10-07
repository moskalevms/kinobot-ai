# init_db.py
# CLI-инициализатор базы данных: тонкая обёртка — миграции схемы + админ.
# Единственная точка эволюции схемы — каталог migrations/ (alembic upgrade
# head через хелпер apply_database_migrations() из src/db_migrations.py),
# источник моделей — src/models/database.py; этот файл схему НЕ определяет
# (ни сырых DDL, ни повторных объявлений колонок — дублей модели нет).
# Все таблицы — включая rt_scores (кэш оценок Rotten Tomatoes, модель
# RtScore), watchlist («📌 Мой список» пользователя, Epic B/B4, модель
# Watchlist), movie_feedback (реакции и оценки пользователя по фильмам,
# Epic B/B5, модель MovieFeedback) и offtopic_refusals (метрики
# офтопик-отказов guardrails, Epic B/B7, модель OfftopicRefusal) —
# создаются миграциями (baseline migrations/versions/0001_initial.py); их
# схемы определены только в src/models/database.py и здесь НЕ дублируются
# (openspec changes add-watchlist, add-movie-feedback,
# add-offtopic-metrics-b7, update-init-db-thin-wrapper-t14).
# Аварийный выключатель оператора RUN_MIGRATIONS=false (также 0/no)
# уважается: хелпер вернёт True БЕЗ наката схемы и без подключения к БД —
# скрипт напечатает нейтральное сообщение о ПРОПУСКЕ (а не об успехе наката:
# случай различается публичным read-only флагом
# db_migrations.migrations_disabled(), логика хелпера здесь не дублируется)
# и продолжит создание админа (схема при этом не изменяется).
import os
import sys

# Добавляем src в путь
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from dotenv import load_dotenv

load_dotenv()

from flask import Flask
from db_migrations import apply_database_migrations, migrations_disabled
from models.database import db, User, Role

# Создаем Flask приложение только для привязки SQLAlchemy
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
    'DATABASE_URL',
    'postgresql://postgres:postgres@localhost:5432/kinobot_db'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)


def init_database():
    """Инициализация базы данных: миграции схемы, затем создание админа"""
    # Схема — только через миграции (alembic upgrade head). Хелпер внутри
    # fail-silent (при сбое пишет ERROR-лог и возвращает False), но семантика
    # CLI-инициализатора — fail-loud: без схемы продолжать бессмысленно,
    # поэтому печатаем ошибку и выходим с кодом 1. Шаг деплоя
    # `docker exec kinobot python init_db.py` использует || true-семантику,
    # поэтому пайплайну ненулевой код не вредит (логируется предупреждение).
    print("🔄 Применение миграций БД (alembic upgrade head)...")
    if not apply_database_migrations():
        print("❌ Не удалось применить миграции БД — детали в ERROR-логе выше.")
        print("   Проверьте DATABASE_URL, доступность БД и наличие файлов")
        print("   (alembic.ini, migrations/), затем запустите инициализацию повторно.")
        sys.exit(1)
    # Хелпер возвращает True и при НАМЕРЕННОМ ПРОПУСКЕ миграций оператором
    # (аварийный выключатель RUN_MIGRATIONS): печатать «миграции применены»
    # в этом случае нельзя — схема БД не накатывалась. Различаем два случая
    # публичным read-only флагом хелпера (его логика здесь не дублируется),
    # значение флага показываем оператору как есть.
    if migrations_disabled():
        print(f"⚠️  Миграции пропущены (RUN_MIGRATIONS={os.getenv('RUN_MIGRATIONS', '')}) — схема БД НЕ изменялась")
    else:
        print("✅ Схема БД актуальна (миграции применены)")

    with app.app_context():
        # Создание роли администратора
        admin_role = Role.query.filter_by(name='admin').first()
        if not admin_role:
            admin_role = Role(name='admin', description='Администратор системы')
            db.session.add(admin_role)
            db.session.commit()
            print("✅ Роль 'admin' создана")
        else:
            print("ℹ️  Роль 'admin' уже существует")

        # Создание пользователя-администратора
        admin_user = User.query.filter_by(username='admin').first()
        if not admin_user:
            admin_password = os.getenv('ADMIN_PASSWORD')
            if not admin_password:
                print("❌ Переменная окружения ADMIN_PASSWORD не задана.")
                print("   Задайте ADMIN_PASSWORD и запустите инициализацию повторно.")
                sys.exit(1)
            admin_user = User(
                username='admin',
                email='admin@kinobot.local',
                role_id=admin_role.id
            )
            admin_user.set_password(admin_password)
            db.session.add(admin_user)
            db.session.commit()
            print("✅ Пользователь 'admin' создан")
            print("   Логин: admin")
        else:
            print("ℹ️  Пользователь 'admin' уже существует")

        print("\n✅ База данных полностью инициализирована!")
        print("🔗 Откройте админ-панель: http://localhost:5000/admin/login")


if __name__ == '__main__':
    print("=" * 60)
    print("  ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ KINOBOT")
    print("=" * 60)
    init_database()
