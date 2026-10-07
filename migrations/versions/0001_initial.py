"""initial — базовая схема kinobot (8 таблиц из src/models/database.py)

Revision ID: 0001
Revises:
Create Date: 2026-10-06 14:50:04.836959

Baseline-миграция (бэклог T11): описывает схему, которая уже существует на
прод-БД (создана init_db.py / db.create_all). Штатный ввод такой БД в
версионирование — `alembic stamp head` (DDL не накатывается вовсе).

Дополнительно миграция БЕЗОПАСНА и идемпотентна при прямом накате: каждый
op.create_table/op.create_index обёрнут проверкой существования объекта
через sa.inspect(op.get_bind()) — «существует → пропускаем» (диалект-
нейтрально, работает и на PostgreSQL, и на SQLite; ручной SQL вида
IF NOT EXISTS не используется). Данные существующих таблиц не изменяются.

Политика миграций проекта (migrations/README.md): additive-first — новые
таблицы/колонки/индексы; разрушающие операции (drop_table/drop_column/
изменение типа) — только после ручного ревью и с явным бэкапом БД.
"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Логгер модуля ревизии (внутренний неймспейс alembic не занимаем);
# сообщения о пропуске существующих объектов — на русском.
logger = logging.getLogger(__name__)

# Идентификаторы ревизии, используются Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector() -> sa.Inspector:
    """Свежий инспектор схемы текущего подключения миграции.

    Инспектор запрашивается при каждой проверке (не кэшируется): внутри
    одной миграции таблицы появляются последовательно, и данные инспекции
    обязаны быть актуальными на момент каждого guard-а.
    """
    return sa.inspect(op.get_bind())


def _table_exists(table: str) -> bool:
    """Существует ли таблица в БД; при существовании пишет лог о пропуске."""
    exists = table in _inspector().get_table_names()
    if exists:
        logger.info('Таблица "%s" уже существует — создание пропускается (baseline идемпотентна)', table)
    return exists


def _index_exists(table: str, index: str) -> bool:
    """Существует ли индекс у таблицы (страховка для частично созданной схемы)."""
    # Один инспектор на обе проверки: таблицы и индексы запрашиваются
    # у одного экземпляра (insp), повторная инспекция не создаётся.
    insp = _inspector()
    if table not in insp.get_table_names():
        return False
    exists = index in {row['name'] for row in insp.get_indexes(table)}
    if exists:
        logger.info('Индекс "%s" таблицы "%s" уже существует — создание пропускается', index, table)
    return exists


def _create_index_if_missing(table: str, index: str, columns: Sequence[str], unique: bool = False) -> None:
    """Создать индекс, если его ещё нет (guard — см. _index_exists)."""
    if _index_exists(table, index):
        return
    op.create_index(op.f(index), table, columns, unique=unique)


def upgrade() -> None:
    """Подъём схемы: все 8 таблиц моделей src/models/database.py.

    Порядок учитывает зависимости: roles создаётся до users (внешний ключ
    users.role_id → roles.id). server_default для TIMESTAMPTZ-колонок —
    CURRENT_TIMESTAMP: валиден и в PostgreSQL, и в SQLite (переносимость;
    в моделях — func.now()). Типы колонок — generic SQLAlchemy, без
    диалект-специфики.
    """
    if not _table_exists('roles'):
        op.create_table('roles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=50), nullable=False),
        sa.Column('description', sa.String(length=200), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name')
        )

    if not _table_exists('users'):
        op.create_table('users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('username', sa.String(length=80), nullable=False),
        sa.Column('email', sa.String(length=120), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('role_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('last_login', sa.DateTime(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email'),
        sa.UniqueConstraint('username')
        )

    if not _table_exists('dialogue_sessions'):
        op.create_table('dialogue_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.String(length=255), nullable=False),
        sa.Column('last_movies', sa.JSON(), nullable=True),
        sa.Column('last_params', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('last_activity', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
        )
    _create_index_if_missing('dialogue_sessions', 'ix_dialogue_sessions_user_id', ['user_id'], unique=True)

    if not _table_exists('rt_scores'):
        op.create_table('rt_scores',
        sa.Column('imdb_id', sa.Text(), nullable=False),
        sa.Column('rt_score', sa.SmallInteger(), nullable=True),
        sa.Column('metascore', sa.SmallInteger(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('imdb_id')
        )

    if not _table_exists('watchlist'):
        op.create_table('watchlist',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.String(length=255), nullable=False),
        sa.Column('kinopoisk_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=500), nullable=False),
        sa.Column('year', sa.Integer(), nullable=True),
        sa.Column('poster_url', sa.String(length=1000), nullable=True),
        sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'kinopoisk_id', name='unique_user_movie')
        )
    _create_index_if_missing('watchlist', 'ix_watchlist_user_id', ['user_id'])

    if not _table_exists('movie_feedback'):
        op.create_table('movie_feedback',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.String(length=255), nullable=False),
        sa.Column('kinopoisk_id', sa.Integer(), nullable=False),
        sa.Column('reaction', sa.String(length=16), nullable=True),
        sa.Column('rating', sa.SmallInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.CheckConstraint('rating IS NULL OR (rating >= 1 AND rating <= 10)', name='movie_feedback_rating_range'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'kinopoisk_id', name='unique_user_movie_feedback')
        )
    _create_index_if_missing('movie_feedback', 'ix_movie_feedback_user_id', ['user_id'])

    if not _table_exists('offtopic_refusals'):
        op.create_table('offtopic_refusals',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.String(length=255), nullable=False),
        sa.Column('reason', sa.String(length=32), nullable=False),
        sa.Column('message_fragment', sa.String(length=120), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id')
        )
    _create_index_if_missing('offtopic_refusals', 'ix_offtopic_refusals_created_at', ['created_at'])
    _create_index_if_missing('offtopic_refusals', 'ix_offtopic_refusals_user_id', ['user_id'])

    if not _table_exists('user_statistics'):
        op.create_table('user_statistics',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.String(length=255), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('user_agent', sa.String(length=500), nullable=True),
        sa.Column('ip_address', sa.String(length=45), nullable=True),
        sa.Column('queries_count', sa.Integer(), nullable=True),
        sa.Column('last_activity', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('session_id', 'date', name='unique_session_date')
        )
    _create_index_if_missing('user_statistics', 'ix_user_statistics_date', ['date'])
    _create_index_if_missing('user_statistics', 'ix_user_statistics_session_id', ['session_id'])


def downgrade() -> None:
    """Откат baseline: удаление всех 8 таблиц.

    ВНИМАНИЕ: вручную на проде НЕ запускать — drop_table уничтожает данные
    пользователей (watchlist, фидбек, статистика). Откат допустим только
    осознанно, после бэкапа БД и ревью (политика — migrations/README.md).
    Индексы и ограничения удаляются вместе с таблицами (отдельные
    drop_index не нужны). Порядок обратный: users до roles (FK).
    """
    op.drop_table('users')
    op.drop_table('dialogue_sessions')
    op.drop_table('watchlist')
    op.drop_table('movie_feedback')
    op.drop_table('offtopic_refusals')
    op.drop_table('user_statistics')
    op.drop_table('rt_scores')
    op.drop_table('roles')
