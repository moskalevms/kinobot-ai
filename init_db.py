# init_db.py
import os
import sys

# Добавляем src в путь
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from dotenv import load_dotenv

load_dotenv()

# Минимальный импорт для инициализации БД
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash
from datetime import datetime, date
from sqlalchemy import func

# Создаем Flask приложение
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
    'DATABASE_URL',
    'postgresql://postgres:postgres@localhost:5432/kinobot_db'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev_secret_key')

db = SQLAlchemy(app)


# Определяем модели прямо здесь
class Role(db.Model):
    __tablename__ = 'roles'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.String(200))

    def __repr__(self):
        return f'<Role {self.name}>'


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role_id = db.Column(db.Integer, db.ForeignKey('roles.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=True)

    role = db.relationship('Role', backref='users')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def __repr__(self):
        return f'<User {self.username}>'


class UserStatistics(db.Model):
    __tablename__ = 'user_statistics'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(255), nullable=False, index=True)
    date = db.Column(db.Date, default=date.today, nullable=False, index=True)
    user_agent = db.Column(db.String(500))
    ip_address = db.Column(db.String(45))
    queries_count = db.Column(db.Integer, default=1)
    last_activity = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('session_id', 'date', name='unique_session_date'),
    )


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
            admin_user = User(
                username='admin',
                email='admin@kinobot.local',
                role_id=admin_role.id
            )
            admin_password = os.getenv('ADMIN_PASSWORD', 'admin123')
            admin_user.set_password(admin_password)
            db.session.add(admin_user)
            db.session.commit()
            print(f"✅ Пользователь 'admin' создан")
            print(f"   Логин: admin")
            print(f"   Пароль: {admin_password}")
        else:
            print("ℹ️  Пользователь 'admin' уже существует")

        print("\n✅ База данных полностью инициализирована!")
        print(f"🔗 Откройте админ-панель: http://localhost:5000/admin/login")


if __name__ == '__main__':
    print("=" * 60)
    print("  ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ KINOBOT")
    print("=" * 60)
    init_database()
