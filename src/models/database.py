# src/models/database.py
from datetime import datetime, date, timezone
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


class RtScore(db.Model):
    """Кэш оценок Rotten Tomatoes/Metacritic из OMDb (Epic B, задача B4).

    imdb_id — первичный ключ (join-ключ из kinopoisk.dev, задача B3).
    rt_score/metascore NULL — «источник не дал оценку»; строка с обоими
    NULL — отрицательный кэш: повторно запрашивать OMDb не нужно.
    fetched_at — момент получения данных: DateTime(timezone=True) даёт
    TIMESTAMPTZ в PostgreSQL и DATETIME в SQLite (переносимость для
    тестов). Значение всегда проставляет rt_cache.set_scores (aware-UTC
    в момент записи/upsert — от него считается TTL); питоновский default
    и server_default=func.now() остаются защитой для вставок в обход ORM
    (ручной SQL, админ-скрипты), чтобы колонка NOT NULL не осталась пустой.

    Единственный источник схемы: migrations/ (alembic upgrade head; init_db.py
    и старт бота накатывают миграции через db_migrations), эта модель —
    источник правды для autogenerate; дублирования определений нет
    (см. openspec change add-rt-cache).
    """
    __tablename__ = 'rt_scores'

    imdb_id = db.Column(db.Text, primary_key=True)
    rt_score = db.Column(db.SmallInteger, nullable=True)
    metascore = db.Column(db.SmallInteger, nullable=True)
    fetched_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f'<RtScore {self.imdb_id} rt={self.rt_score} meta={self.metascore}>'


class Watchlist(db.Model):
    """Список сохранённых фильмов пользователя «📌 Мой список» (Epic B, задача B4).

    Пара (user_id, kinopoisk_id) уникальна на уровне схемы — повторное
    сохранение того же фильма не создаёт вторую запись (идемпотентность
    гарантируется БД, а не приложением: гонка двух одновременных тапов
    безопасна). user_id — строковый id из str(update.effective_user.id),
    согласованно с DialogueSession.user_id.

    added_at — момент добавления: DateTime(timezone=True) даёт TIMESTAMPTZ
    в PostgreSQL и DATETIME в SQLite (переносимость для тестов), значение
    всегда проставляет ORM (aware-UTC), server_default=func.now() — защита
    для вставок в обход ORM, чтобы колонка NOT NULL не осталась пустой
    (образец — RtScore.fetched_at).

    Единственный источник схемы: migrations/ (alembic upgrade head; init_db.py
    и старт бота накатывают миграции через db_migrations), эта модель —
    источник правды для autogenerate; дублирования определений нет
    (см. openspec change add-watchlist).
    """
    __tablename__ = 'watchlist'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(255), nullable=False, index=True)
    kinopoisk_id = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(500), nullable=False)
    year = db.Column(db.Integer, nullable=True)
    poster_url = db.Column(db.String(1000), nullable=True)
    added_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint('user_id', 'kinopoisk_id', name='unique_user_movie'),
    )

    def __repr__(self) -> str:
        return f'<Watchlist user={self.user_id} film={self.kinopoisk_id} «{self.title}»>'


class MovieFeedback(db.Model):
    """Обратная связь пользователя по фильму: реакция и/или оценка (Epic B, задача B5).

    Ряд фидбека карточки — «⭐ Оценить» (панель 1–10), «✅ Смотрел» и
    «❌ Не моё» — пишется ОДНОЙ строкой на пару (user_id, kinopoisk_id):
    пара уникальна на уровне схемы, повторный тап ОБНОВЛЯет строку
    (upsert-семантика менеджера), а не плодит дубли. reaction
    ('watched'/'nope') и rating (1–10) — независимые колонки одного
    состояния пользователя по фильму (NULL — значение не оставлено);
    диапазон оценки дополнительно закреплён CHECK-ограничением
    (переносимо: соблюдается и в PostgreSQL, и в SQLite-тестах).
    user_id — строковый id из str(update.effective_user.id), согласованно
    с Watchlist.user_id/DialogueSession.user_id.

    created_at/updated_at — DateTime(timezone=True): TIMESTAMPTZ в
    PostgreSQL и DATETIME в SQLite (переносимость для тестов), значения
    проставляет ORM (aware-UTC), server_default=func.now() — защита для
    вставок в обход ORM (образец — RtScore.fetched_at). updated_at
    обновляется при каждом изменении строки (onupdate) — свежесть
    фидбека для будущей персонализации (задача C5; ранжирование в B5
    не меняется).

    Единственный источник схемы: migrations/ (alembic upgrade head; init_db.py
    и старт бота накатывают миграции через db_migrations), эта модель —
    источник правды для autogenerate; дублирования определений нет
    (см. openspec change add-movie-feedback).
    """
    __tablename__ = 'movie_feedback'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(255), nullable=False, index=True)
    kinopoisk_id = db.Column(db.Integer, nullable=False)
    reaction = db.Column(db.String(16), nullable=True)
    rating = db.Column(db.SmallInteger, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint('user_id', 'kinopoisk_id', name='unique_user_movie_feedback'),
        db.CheckConstraint(
            'rating IS NULL OR (rating >= 1 AND rating <= 10)',
            name='movie_feedback_rating_range',
        ),
    )

    def __repr__(self) -> str:
        return (
            f'<MovieFeedback user={self.user_id} film={self.kinopoisk_id} '
            f'reaction={self.reaction} rating={self.rating}>'
        )


class OfftopicRefusal(db.Model):
    """Метрики офтопик-отказов guardrails (Epic B, задача B7).

    Строка — ОДНО событие отказа: precheck (причины 'length',
    'prompt_attack', 'offtopic') или отказ LLM-классификатора
    ('llm_offtopic'). message_fragment — ПРИВАТНОСТЬ: хранится только
    усечённый фрагмент сообщения (≤120 символов, та же граница, что в
    guardrails.log_blocked), полный ввод и системный промпт в БД не
    попадают. user_id — строковый id из str(update.effective_user.id)
    (бот) или ключ веб-сессии, согласованно с DialogueSession.user_id.

    created_at — момент фиксации: DateTime(timezone=True) даёт TIMESTAMPTZ
    в PostgreSQL и DATETIME в SQLite (переносимость для тестов), значение
    всегда проставляет ORM (aware-UTC), server_default=func.now() —
    защита для вставок в обход ORM (образец — RtScore.fetched_at).
    Индекс по created_at — фильтру периода в агрегации топ-N отказов.

    Единственный источник схемы: migrations/ (alembic upgrade head; init_db.py
    и старт бота накатывают миграции через db_migrations), эта модель —
    источник правды для autogenerate; дублирования определений нет
    (см. openspec change add-offtopic-metrics-b7).
    """
    __tablename__ = 'offtopic_refusals'

    # Граница усечения фрагмента — единый источник для модели и трекера
    # записи (refusal_tracker.record_refusal); совпадает с log_blocked.
    FRAGMENT_LIMIT = 120

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(255), nullable=False, index=True)
    reason = db.Column(db.String(32), nullable=False)
    message_fragment = db.Column(db.String(FRAGMENT_LIMIT), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    @staticmethod
    def get_top_fragments(cutoff, limit=50):
        """Топ-N заблокированных фрагментов за период (для админки, B7).

        Группировка по (фрагмент, причина): одинаковые посторонние
        запросы разных пользователей дают одну строку со счётчиком —
        повторяющиеся ложные срабатывания видны по данным, а не по
        жалобам. last_seen — момент последней фиксации (max created_at).
        Фильтр created_at >= cutoff отсекает записи вне периода.
        Порядок детерминирован: count desc, затем last_seen desc —
        при равных счётчиках топ-N не «мерцает» между запросами
        (стандартные count/max переносимы для SQLite и PostgreSQL).
        Возвращает строки-кортежи (fragment, reason, count, last_seen) —
        примитивы/detached-значения, безопасные вне SQLAlchemy-сессии.
        Вызывается внутри app_context (образец — get_daily_stats).
        """
        return db.session.query(
            OfftopicRefusal.message_fragment,
            OfftopicRefusal.reason,
            func.count(OfftopicRefusal.id).label('total'),
            func.max(OfftopicRefusal.created_at).label('last_seen'),
        ).filter(
            OfftopicRefusal.created_at >= cutoff
        ).group_by(
            OfftopicRefusal.message_fragment,
            OfftopicRefusal.reason,
        ).order_by(
            func.count(OfftopicRefusal.id).desc(),
            func.max(OfftopicRefusal.created_at).desc(),
        ).limit(limit).all()

    def __repr__(self) -> str:
        return f'<OfftopicRefusal user={self.user_id} reason={self.reason} «{self.message_fragment[:30]}»>'


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
