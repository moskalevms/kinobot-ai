# src/models/database.py
from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func
from flask_login import UserMixin

db = SQLAlchemy()


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

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.username}>'


class Role(db.Model):
    __tablename__ = 'roles'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.String(200))

    def __repr__(self):
        return f'<Role {self.name}>'


class DialogueSession(db.Model):
    """Сессия диалога пользователя: последние рекомендации и параметры поиска"""
    __tablename__ = 'dialogue_sessions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(255), unique=True, nullable=False, index=True)
    last_movies = db.Column(db.JSON, default=list)
    last_params = db.Column(db.JSON, default=dict)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_activity = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<DialogueSession {self.user_id}>'


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

    @staticmethod
    def track_user(session_id, user_agent=None, ip_address=None):
        """Отслеживание активности пользователя"""
        today = date.today()
        stat = UserStatistics.query.filter_by(
            session_id=session_id,
            date=today
        ).first()

        if stat:
            stat.queries_count += 1
            stat.last_activity = datetime.utcnow()
        else:
            stat = UserStatistics(
                session_id=session_id,
                date=today,
                user_agent=user_agent,
                ip_address=ip_address,
                queries_count=1
            )
            db.session.add(stat)

        db.session.commit()

    @staticmethod
    def get_daily_stats(start_date=None, end_date=None):
        """Получить статистику по дням"""
        query = db.session.query(
            UserStatistics.date,
            func.count(func.distinct(UserStatistics.session_id)).label('unique_users'),
            func.sum(UserStatistics.queries_count).label('total_queries')
        ).group_by(UserStatistics.date)

        if start_date:
            query = query.filter(UserStatistics.date >= start_date)
        if end_date:
            query = query.filter(UserStatistics.date <= end_date)

        return query.order_by(UserStatistics.date.desc()).all()

    @staticmethod
    def get_monthly_stats():
        """Получить статистику по месяцам"""
        return db.session.query(
            func.date_trunc('month', UserStatistics.date).label('month'),
            func.count(func.distinct(UserStatistics.session_id)).label('unique_users'),
            func.sum(UserStatistics.queries_count).label('total_queries')
        ).group_by('month').order_by('month').all()
