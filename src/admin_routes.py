# src/admin_routes.py
from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_user, logout_user, login_required, current_user
from functools import wraps
from datetime import datetime, timedelta, date
from models.database import db, User, Role, UserStatistics

admin_bp = Blueprint('admin', __name__)


def admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if current_user.role.name != 'admin':
            flash('Доступ запрещен', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)

    return decorated_function


@admin_bp.route('/login', methods=['GET', 'POST'])
def admin_login():
    if current_user.is_authenticated:
        return redirect(url_for('admin.dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password) and user.role.name == 'admin':
            login_user(user)
            user.last_login = datetime.utcnow()
            db.session.commit()
            flash('Успешный вход', 'success')
            return redirect(url_for('admin.dashboard'))
        else:
            flash('Неверное имя пользователя или пароль', 'danger')

    return render_template('admin/login.html')


@admin_bp.route('/logout')
@login_required
def admin_logout():
    logout_user()
    flash('Вы вышли из системы', 'info')
    return redirect(url_for('admin.admin_login'))


@admin_bp.route('/dashboard')
@admin_required
def dashboard():
    """Главная страница статистики"""
    # Статистика за сегодня
    today = date.today()
    today_stats = UserStatistics.query.filter_by(date=today).all()
    today_unique = len(set(s.session_id for s in today_stats))
    today_queries = sum(s.queries_count for s in today_stats)

    # Статистика за последние 30 дней
    month_ago = today - timedelta(days=30)
    daily_stats = UserStatistics.get_daily_stats(start_date=month_ago, end_date=today)

    # Статистика за текущий месяц
    month_start = date(today.year, today.month, 1)
    month_stats = UserStatistics.query.filter(UserStatistics.date >= month_start).all()
    month_unique = len(set(s.session_id for s in month_stats))
    month_queries = sum(s.queries_count for s in month_stats)

    # Статистика за все время
    all_stats = UserStatistics.query.all()
    total_unique = len(set(s.session_id for s in all_stats))
    total_queries = sum(s.queries_count for s in all_stats)

    return render_template('admin/dashboard.html',
                           today_unique=today_unique,
                           today_queries=today_queries,
                           month_unique=month_unique,
                           month_queries=month_queries,
                           total_unique=total_unique,
                           total_queries=total_queries,
                           daily_stats=daily_stats)


@admin_bp.route('/statistics')
@admin_required
def statistics():
    """Детальная статистика"""
    # Параметры фильтрации
    days = request.args.get('days', 30, type=int)
    end_date = date.today()
    start_date = end_date - timedelta(days=days)

    # Получение данных
    daily_stats = UserStatistics.get_daily_stats(start_date=start_date, end_date=end_date)

    # Подготовка данных для графиков
    chart_data = {
        'dates': [str(stat.date) for stat in reversed(daily_stats)],
        'unique_users': [stat.unique_users for stat in reversed(daily_stats)],
        'total_queries': [stat.total_queries for stat in reversed(daily_stats)]
    }

    return render_template('admin/statistics.html',
                           chart_data=chart_data,
                           days=days)


@admin_bp.route('/users')
@admin_required
def users():
    """Управление пользователями"""
    all_users = User.query.all()
    return render_template('admin/users.html', users=all_users)
