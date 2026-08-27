# init_db.py
# Единая точка инициализации базы данных: модели берутся из
# src/models/database.py, дублирующих определений нет.
import os
import sys

# Добавляем src в путь
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from dotenv import load_dotenv

load_dotenv()

from flask import Flask
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
    """Инициализация базы данных"""
    with app.app_context():
        print("🔄 Создание таблиц...")
        db.create_all()
        print("✅ Таблицы созданы")

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
